"""Exercise the generated prompt timer in a real, isolated QML engine."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from runtime import _PROMPT_TIMEOUT


@unittest.skipUnless(os.environ.get('LOCKSAVER_QML_RUNNER'), 'Run bin/test to build the isolated Qt QML runner')
class PromptTimeoutTests(unittest.TestCase):
    def test_activity_authentication_and_return_without_waking_display(self):
        with tempfile.TemporaryDirectory() as directory:
            source = '''import QtQuick
Item {
  Item {
    id: root
    property bool inputEnabled: true
    property bool saverDismissed: true
    property bool authenticatingPassword: false
    property string password: "unfinished"
    property string failure: "previous failure"
    property int wakes: 0
    signal wakeRequested()
    signal passwordTextEdited(string value)
    signal clearFailureRequested()
    onPasswordTextEdited: value => password = value
    onClearFailureRequested: failure = ""
    Connections { target: root; function onWakeRequested() { root.wakes++; root.promptActivitySerial++ } }
''' + _PROMPT_TIMEOUT.replace('interval: 30000', 'interval: 150') + '''
    Timer {
      interval: 90; running: true
      onTriggered: root.wakeRequested()
    }
    Timer {
      interval: 190; running: true
      onTriggered: {
        if (!root.saverDismissed) throw new Error("activity did not reset countdown")
        root.authenticatingPassword = true
      }
    }
    Timer {
      interval: 400; running: true
      onTriggered: {
        if (!root.saverDismissed) throw new Error("timed out during authentication")
        root.authenticatingPassword = false
      }
    }
    Timer {
      interval: 650; running: true
      onTriggered: {
        if (root.saverDismissed || root.password !== "" || root.failure !== "" || root.wakes !== 1)
          throw new Error("incorrect saver return or display countdown reset")
        console.log("prompt-timeout-passed")
        Qt.quit()
      }
    }
  }
}
'''
            Path(directory, 'shell.qml').write_text(source)
            result = subprocess.run([os.environ['LOCKSAVER_QML_RUNNER'], '--qml-test', str(Path(directory, 'shell.qml'))],
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                         'QT_QPA_PLATFORMTHEME': '', 'QT_FORCE_STDERR_LOGGING': '1', 'QT_LOGGING_RULES': 'qml.debug=true', 'XDG_CACHE_HOME': directory,
                                         'XDG_RUNTIME_DIR': directory},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('prompt-timeout-passed', result.stdout)
            self.assertNotIn('Error:', result.stdout)

    def run_qml(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'probe.qml')
            path.write_text(source)
            result = subprocess.run([os.environ['LOCKSAVER_QML_RUNNER'], '--qml-test', str(path)],
                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QT_QPA_PLATFORMTHEME': '', 'QT_FORCE_STDERR_LOGGING': '1', 'QT_LOGGING_RULES': 'qml.debug=true'},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('prompt-probe-passed', result.stdout)
            self.assertNotIn('Error:', result.stdout)

    def test_disabled_input_and_repeated_timeout_cycles(self):
        source = """import QtQuick
Item {
  id: root
  property bool inputEnabled: false
  property bool saverDismissed: true
  property bool authenticatingPassword: false
  property string password: "first"
  property string failure: "failure"
  signal passwordTextEdited(string value)
  signal clearFailureRequested()
  onPasswordTextEdited: value => password = value
  onClearFailureRequested: failure = ""
""" + _PROMPT_TIMEOUT.replace('interval: 30000', 'interval: 100') + """
  Timer { interval: 180; running: true; onTriggered: {
    if (!root.saverDismissed || root.password !== "first") throw new Error("disabled input timed out")
    root.inputEnabled = true
  } }
  Timer { interval: 380; running: true; onTriggered: {
    if (root.saverDismissed || root.password !== "" || root.failure !== "") throw new Error("first timeout failed")
    root.password = "second"; root.saverDismissed = true; root.promptActivitySerial++
  } }
  Timer { interval: 580; running: true; onTriggered: {
    if (root.saverDismissed || root.password !== "") throw new Error("repeat timeout failed")
    console.log("prompt-probe-passed"); Qt.quit()
  } }
}
"""
        self.run_qml(source)

    def test_activity_on_one_output_resets_all_outputs_without_wake_on_timeout(self):
        source = """import QtQuick
Item {
  id: service
  property string password: "unfinished"
  property bool authenticating: false
  property int activity: 0
  property int wakes: 0
  component Prompt: Item {
    id: root
    property bool inputEnabled: true
    property bool saverDismissed: true
    property bool authenticatingPassword: service.authenticating
    signal wakeRequested()
    signal passwordTextEdited(string value)
    signal clearFailureRequested()
    onWakeRequested: { service.wakes++; service.activity++ }
    onPasswordTextEdited: value => service.password = value
""" + _PROMPT_TIMEOUT.replace('interval: 30000', 'interval: 200') + """
  }
  Prompt { id: first; promptActivitySerial: service.activity }
  Prompt { id: second; promptActivitySerial: service.activity }
  Timer { interval: 120; running: true; onTriggered: first.wakeRequested() }
  Timer { interval: 240; running: true; onTriggered: {
    if (service.password !== "unfinished") throw new Error("second output cleared active password")
    first.wakeRequested()
  } }
  Timer { interval: 330; running: true; onTriggered: {
    if (!first.saverDismissed || !second.saverDismissed || service.password !== "unfinished") throw new Error("activity not shared")
    service.authenticating = true
  } }
  Timer { interval: 570; running: true; onTriggered: {
    if (service.password !== "unfinished") throw new Error("timeout during authentication")
    service.authenticating = false
  } }
  Timer { interval: 850; running: true; onTriggered: {
    if (first.saverDismissed || second.saverDismissed || service.password !== "" || service.wakes !== 2) throw new Error("first shared timeout failed")
    service.password = "again"; first.saverDismissed = true; second.saverDismissed = true; first.wakeRequested()
  } }
  Timer { interval: 1150; running: true; onTriggered: {
    if (first.saverDismissed || second.saverDismissed || service.password !== "" || service.wakes !== 3) throw new Error("repeat shared timeout failed")
    console.log("prompt-probe-passed"); Qt.quit()
  } }
}
"""
        self.run_qml(source)
