import QtQuick
import Quickshell
import Quickshell.Io

// Select exactly once at startup. Never switch a live session-lock client in
// response to file changes: that can strand a locked compositor.
Item {
  id: root
  property var shell: null
  property string omarchyPath: Quickshell.env("OMARCHY_PATH") || "/usr/share/omarchy"
  property string sourceKind: "stock"
  property string loadError: ""
  readonly property string stockUrl: "file://" + omarchyPath + "/shell/plugins/lock/Service.qml"
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || (Quickshell.env("HOME") + "/.local/state")

  function injectContext() {
    if (!implementation.item) return
    if ("shell" in implementation.item) implementation.item.shell = root.shell
    if ("omarchyPath" in implementation.item) implementation.item.omarchyPath = root.omarchyPath
  }
  onShellChanged: injectContext()
  onOmarchyPathChanged: injectContext()

  FileView {
    id: activeFile
    path: root.stateHome + "/omarchy-lock-screensaver/active.json"
    blockLoading: true
    watchChanges: false
    printErrors: false
  }

  Loader {
    id: implementation
    onLoaded: root.injectContext()
    onStatusChanged: {
      if (status === Loader.Error && root.sourceKind === "custom") {
        root.loadError = "Custom runtime could not load; using the installed stock lock screen"
        console.warn("lock-screensaver:", root.loadError)
        // No custom component was instantiated, so no session lock is destroyed.
        root.sourceKind = "stock"
        source = root.stockUrl
      }
    }
  }

  Component.onCompleted: {
    let selected = root.stockUrl
    try {
      let pointer = JSON.parse(activeFile.text() || "{}")
      if (typeof pointer.serviceUrl === "string" && pointer.serviceUrl.indexOf("file:///") === 0) {
        selected = pointer.serviceUrl
        root.sourceKind = "custom"
      }
    } catch (error) {
      console.warn("lock-screensaver: no usable runtime; using installed stock lock")
    }
    implementation.source = selected
  }

  IpcHandler {
    target: "lock-screensaver"
    function status(): string {
      return JSON.stringify({ready: implementation.status === Loader.Ready,
                             custom: root.sourceKind === "custom", error: root.loadError})
    }
  }
}
