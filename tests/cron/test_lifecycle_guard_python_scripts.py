"""Python cron scripts are checked as Python, not tokenized as shell."""
import pytest

from cron.lifecycle_guard import GatewayLifecycleBlocked, check_gateway_lifecycle

PROBE = """import os, signal, time
signal.alarm(20)
for d in ("Downloads", "Desktop"):
    print(len(os.listdir(os.path.expanduser("~/" + d))))
"""


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    return tmp_path / "scripts"


def test_python_string_literal_path_is_not_a_referenced_script(scripts):
    (scripts / "probe.py").write_text(PROBE)
    check_gateway_lifecycle("", "probe.py")


@pytest.mark.parametrize("body", [
    "import subprocess\nsubprocess.run([\x27launchctl\x27, \x27submit\x27, \x27-l\x27, \x27x\x27, \x27--\x27, \x27/bin/sh\x27])\n",
    "import os\nos.system(\x27launchctl bootstrap gui/501 /tmp/x.plist\x27)\n",
    "import os\nos.system(\x27hermes gateway restart\x27)\n",
])
def test_python_lifecycle_and_submit_are_still_blocked(scripts, body):
    (scripts / "bad.py").write_text(body)
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle("", "bad.py")


def test_shell_script_referencing_a_directory_is_not_blocked(scripts, tmp_path):
    (scripts / "ok.sh").write_text(f"ls {tmp_path}/\n")
    check_gateway_lifecycle("", "ok.sh")


def test_shell_script_lifecycle_is_still_blocked(scripts):
    (scripts / "bad.sh").write_text("launchctl kickstart -k gui/501/ai.hermes.gateway\n")
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle("", "bad.sh")
