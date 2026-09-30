"""Profile-scoped OpenViking actor precedence regressions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import plugins.memory.openviking as openviking


class OpenVikingActorTests(unittest.TestCase):
    def test_memory_config_agent_is_used_for_actor_header(self):
        config = {"memory": {"openviking": {"agent": "observer"}}}
        with patch.dict(os.environ, {}, clear=True), patch(
            "hermes_cli.config.load_config_readonly", return_value=config
        ):
            settings = openviking._resolve_connection_settings(
                openviking._load_hermes_openviking_config()
            )
        self.assertEqual(settings["agent"], "observer")
        with patch.object(openviking, "_get_httpx", return_value=object()):
            client = openviking._VikingClient(**settings)
        self.assertEqual(client._headers()["X-OpenViking-Actor-Peer"], "observer")

    def test_profile_agent_overrides_shared_environment_and_linked_actor(self):
        with tempfile.TemporaryDirectory() as temp:
            linked = Path(temp) / "ovcli.conf"
            linked.write_text(json.dumps({"actor_peer_id": "linked", "url": "http://linked.local"}))
            config = {"agent": "sherlock", "use_ovcli_config": True, "ovcli_config_path": str(linked)}
            with patch.dict(os.environ, {"OPENVIKING_AGENT": "shared"}, clear=True):
                settings = openviking._resolve_connection_settings(config)
                other = openviking._resolve_connection_settings({**config, "agent": "tars"})
            self.assertEqual(settings["agent"], "sherlock")
            self.assertEqual(other["agent"], "tars")
            self.assertEqual(settings["endpoint"], "http://linked.local")

    def test_environment_then_linked_actor_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp:
            linked = Path(temp) / "ovcli.conf"
            linked.write_text(json.dumps({"actor_peer_id": "linked"}))
            config = {"use_ovcli_config": True, "ovcli_config_path": str(linked)}
            with patch.dict(os.environ, {"OPENVIKING_AGENT": "shared"}, clear=True):
                self.assertEqual(openviking._resolve_connection_settings(config)["agent"], "shared")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(openviking._resolve_connection_settings(config)["agent"], "linked")

    def test_missing_or_empty_actor_uses_hermes_default(self):
        with patch.dict(os.environ, {}, clear=True):
            for config in ({}, {"agent": ""}, {"agent": None}):
                self.assertEqual(openviking._resolve_connection_settings(config)["agent"], "hermes")
