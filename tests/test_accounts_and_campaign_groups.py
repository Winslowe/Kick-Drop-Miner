import os
import tempfile
import unittest
from unittest.mock import patch

import core.api as api
from core.accounts import AccountStore, hash_password
from core.service import MinerService


class AccountIsolationTests(unittest.TestCase):
    def test_bootstrap_admin_updates_credentials_without_changing_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = AccountStore(os.path.join(root, "accounts.sqlite3"))
            original = store.bootstrap_admin(
                "old-admin",
                hash_password("old-password"),
            )
            updated = store.bootstrap_admin(
                "admin",
                hash_password("samurosakiAdmin"),
            )

            self.assertEqual(updated["id"], original["id"])
            self.assertEqual(updated["username"], "admin")
            self.assertIsNone(
                store.authenticate("old-admin", "old-password", "127.0.0.1")
            )
            self.assertEqual(
                store.authenticate(
                    "admin",
                    "samurosakiAdmin",
                    "127.0.0.1",
                )["role"],
                "admin",
            )

    def test_accounts_close_database_and_keep_user_data_separate(self):
        with tempfile.TemporaryDirectory() as root:
            store = AccountStore(os.path.join(root, "accounts.sqlite3"))
            admin = store.bootstrap_admin(
                "admin",
                hash_password("admin-password"),
            )
            user = store.create_user(
                "tester",
                "tester@example.com",
                "password123",
            )
            first = MinerService(
                data_dir=os.path.join(root, "admin"),
                user_id=admin["id"],
                username=admin["username"],
            )
            second = MinerService(
                data_dir=os.path.join(root, "user"),
                user_id=user["id"],
                username=user["username"],
            )
            first.add_stream("https://kick.com/example", 10)

            self.assertEqual(len(first.snapshot()["items"]), 1)
            self.assertEqual(len(second.snapshot()["items"]), 0)
            self.assertNotEqual(first.cookie_path, second.cookie_path)
            self.assertNotEqual(first.profile_prefix, second.profile_prefix)


class CampaignGroupingTests(unittest.TestCase):
    def test_known_rust_rewards_use_official_facepunch_art(self):
        campaigns = api._parse_campaigns(
            [
                {
                    "id": "campaign",
                    "name": "Team hJune + Frost AR",
                    "status": "active",
                    "category": {"id": 13, "name": "Rust"},
                    "rewards": [
                        {
                            "id": "reward",
                            "name": "Team hJune + Frost AR",
                            "image_url": "drops/reward-image/fallback.png",
                        }
                    ],
                    "channels": [],
                }
            ]
        )

        reward = campaigns[0]["rewards"][0]
        self.assertEqual(
            reward["image_url"],
            "https://files.facepunch.com/lewis/1b0111b1/ak47.jpg",
        )
        self.assertEqual(
            reward["kick_image_url"],
            "drops/reward-image/fallback.png",
        )

    def test_browser_crash_retries_same_channel_before_rotating(self):
        class FinishedWorker:
            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

            def force_close_driver(self):
                return None

        with tempfile.TemporaryDirectory() as root:
            service = MinerService(data_dir=root, user_id="user")
            service.add_stream(
                "https://kick.com/one",
                0,
                campaign_id="campaign",
                campaign_channels=[
                    {"url": "https://kick.com/one", "username": "one"},
                    {"url": "https://kick.com/two", "username": "two"},
                ],
            )
            item = service.config.items[0]
            service._states[item["id"]] = {}

            service._on_worker_finish(
                FinishedWorker(),
                item["id"],
                0,
                0,
                False,
                "browser_error",
            )

            self.assertEqual(item["url"], "https://kick.com/one")
            self.assertEqual(
                item["browser_retry_counts"]["https://kick.com/one"],
                1,
            )
            self.assertEqual(item.get("tried_channels"), [])

    def test_skipped_channel_exposes_transition_reason(self):
        with tempfile.TemporaryDirectory() as root:
            service = MinerService(data_dir=root, user_id="user")
            service.add_stream(
                "https://kick.com/offline",
                0,
                campaign_id="campaign",
                campaign_channels=[
                    {"url": "https://kick.com/offline", "username": "offline"}
                ],
            )
            item = service.config.items[0]
            item["last_transition"] = {
                "reason": "offline",
                "label": "Çevrimdışı",
                "message": "Kanal çevrimdışı olduğu için geçildi.",
            }

            snapshot = service.snapshot()["items"][0]

            self.assertEqual(snapshot["transition"]["reason"], "offline")
            self.assertIn("geçildi", snapshot["transition"]["message"])

    def test_waiting_task_keeps_persisted_kick_progress(self):
        with tempfile.TemporaryDirectory() as root:
            service = MinerService(data_dir=root, user_id="user")
            service.add_stream(
                "https://kick.com/example",
                0,
                campaign_id="campaign",
            )
            item = service.config.items[0]
            item["drop_progress"] = 42.5
            item["drop_verified"] = True

            snapshot = service.snapshot()["items"][0]

            self.assertEqual(snapshot["progress_percent"], 42.5)
            self.assertEqual(snapshot["drop_progress"], 42.5)
            self.assertTrue(snapshot["drop_verified"])

    def test_inventory_cache_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as root:
            service = MinerService(data_dir=root, user_id="user")
            service._inventory = [{"id": "campaign", "name": "Rust Drop"}]
            service._inventory_progress = [{"id": "campaign", "percentage": 35}]
            service._inventory_updated_at = "2026-06-12T20:00:00+00:00"
            service._save_inventory_cache()

            restored = MinerService(data_dir=root, user_id="user")
            inventory = restored.snapshot()["inventory"]

            self.assertEqual(inventory["campaigns"][0]["id"], "campaign")
            self.assertEqual(inventory["progress"][0]["percentage"], 35)
            self.assertEqual(
                inventory["updated_at"],
                "2026-06-12T20:00:00+00:00",
            )

    def test_campaign_adds_one_task_with_alternative_channels(self):
        with tempfile.TemporaryDirectory() as root:
            service = MinerService(
                data_dir=root,
                user_id="user",
                username="tester",
            )
            service._inventory = [
                {
                    "id": "campaign",
                    "name": "Rust Drop",
                    "game": "Rust",
                    "category_id": 12,
                    "game_image": "rust/banner.png",
                    "rewards": [
                        {
                            "id": "reward-one",
                            "name": "Garage Door",
                            "image_url": "rust/reward-one.png",
                        },
                        {
                            "id": "reward-two",
                            "name": "Crossbow",
                            "image_url": "rust/reward-two.png",
                        },
                    ],
                    "channels": [
                        {
                            "url": "https://kick.com/one",
                            "username": "one",
                            "profile_picture": "one.png",
                        },
                        {
                            "url": "https://kick.com/two",
                            "username": "two",
                            "profile_picture": "two.png",
                        },
                    ],
                }
            ]
            service._inventory_progress = [
                {
                    "id": "campaign",
                    "rewards": [
                        {"id": "reward-one", "progress": 100, "claimed": True},
                        {"id": "reward-two", "progress": 25, "claimed": False},
                    ],
                }
            ]

            result = service.add_campaign("campaign")
            state = service.snapshot()

            self.assertEqual(result, {"added": 1, "skipped": 0})
            self.assertEqual(len(state["items"]), 1)
            self.assertEqual(len(state["items"][0]["campaign_channels"]), 2)
            self.assertEqual(
                state["items"][0]["reward_image"],
                "rust/reward-two.png",
            )

            repeated = service.add_campaign("campaign")
            self.assertEqual(repeated["added"], 0)
            self.assertEqual(len(service.snapshot()["items"]), 1)

    def test_public_http_headers_do_not_leak_default_account_cookie(self):
        with tempfile.TemporaryDirectory() as root:
            cookie_path = os.path.join(root, "kick.com.json")
            with open(cookie_path, "w", encoding="utf-8") as cookie_file:
                cookie_file.write(
                    '[{"name":"session_token","value":"private-token"}]'
                )
            with patch.object(
                api,
                "cookie_file_for_domain",
                return_value=cookie_path,
            ):
                headers = api._http_headers(authenticated=False)

            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
