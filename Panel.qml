import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// h3nr1.d14z — AI Usage Global.
// Bar-widget entry point (manifest: entryPoints.barWidget = "Panel.qml"), same
// single-file pattern the four reference plugins use: this file is both the bar
// chip and the popup panel. Data comes from engine/usage.py, which prints one
// JSON document (quota windows per provider + local token consumption). Click
// opens the panel; right/middle-click or 'R' refreshes; Esc closes; Tab walks
// providers. The 1s nowMs ticker keeps every reset countdown honest while open.
Panel {
  id: root
  moduleName: "h3nr1.d14z.ai-usage"
  ipcTarget: "h3nr1.d14z.ai-usage"
  manageIpc: false

  implicitWidth: root.barShowsData ? Math.max(dataButton.implicitWidth, Style.space(120)) : button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- shell theme, with safe fallbacks when the bar is not yet injected ----
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  // ---- data plane ---------------------------------------------------------- //
  property var providers: []
  property var local: ({})
  property bool loading: true
  property string errorText: ""
  property int selectedProvider: 0
  property int viewTab: 0   // 0 = subscriptions, 1 = consumption

  // Ticks once a second while the shell is alive so countdowns stay honest.
  property double nowMs: Date.now()
  Timer { interval: 1000; running: true; repeat: true; onTriggered: root.nowMs = Date.now() }

  readonly property int refreshIntervalSec: Math.max(60, Number(root.setting("refreshIntervalSec", 900)) || 900)
  readonly property string barDisplay: String(root.setting("barDisplay", "Data"))
  readonly property bool barShowsData: barDisplay.toLowerCase() === "data"
  readonly property string defaultProviderId: String(root.setting("defaultProvider", "opencode"))

  readonly property var configuredProviders: (root.providers || []).filter(function (p) { return p && p.configured })
  readonly property var shownProviders: configuredProviders.length > 0 ? configuredProviders : (root.providers || [])
  readonly property var selected: shownProviders.length > 0
    ? shownProviders[Math.min(selectedProvider, shownProviders.length - 1)] : null

  readonly property bool alarming: {
    var list = root.configuredProviders
    for (var i = 0; i < list.length; i++) {
      var wins = list[i].windows || []
      for (var j = 0; j < wins.length; j++)
        if (wins[j].percent !== null && wins[j].percent >= 90) return true
    }
    return false
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function providerById(id) {
    var list = root.providers || []
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i]
    return null
  }
  function defaultRecord() {
    var want = root.providerById(root.defaultProviderId)
    if (want && want.configured) return want
    var c = root.configuredProviders
    return c.length > 0 ? c[0] : null
  }

  // Compact one-liner for the bar chip in Data mode: headline + nearest reset.
  function compactChip(p) {
    if (!p) return "AI —"
    if (p.kind === "balance" && p.label) return (p.display || p.name) + " " + p.label
    var wins = (p.windows || []).filter(function (w) { return w && w.percent !== null })
    if (wins.length === 0) return (p.display || "") + " —"
    var parts = []
    for (var i = 0; i < wins.length; i++)
      parts.push(wins[i].label + " " + Math.round(wins[i].percent) + "%")
    return (p.display || "") + " " + parts.join(" · ")
  }

  function resetRemainingMs(iso) {
    if (!iso) return -1
    var ms = Date.parse(String(iso).replace(" ", "T"))
    return isFinite(ms) ? ms - root.nowMs : -1
  }
  function formatReset(ms) {
    if (!(ms > 0)) return "—"
    var m = Math.floor(ms / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24)
    if (d > 0) return d + "d " + (h % 24) + "h"
    if (h > 0) return h + "h " + (m % 60) + "m"
    return Math.max(1, m) + "m"
  }
  function humanTokens(n) {
    n = Number(n) || 0
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B"
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M"
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k"
    return String(n)
  }

  // Tone thresholds mirror the reference plugins: accent ≥70%, urgent ≥90%.
  function tone(ratio) {
    if (!(ratio >= 0)) return root.dim
    if (ratio >= 0.9) return root.urgent
    if (ratio >= 0.7) return root.accent
    return root.foreground
  }

  // ---- refresh / ipc ------------------------------------------------------- //
  function refresh() { if (!engine.running) engine.running = true }
  function scriptPath() { return decodeURIComponent(Qt.resolvedUrl("engine/usage.py").toString().replace(/^file:\/\//, "")) }
  function settingsJson() {
    return JSON.stringify({
      refreshIntervalSec: root.refreshIntervalSec,
      showLocalConsumption: String(root.setting("showLocalConsumption", "On")).toLowerCase() === "on",
      qwenPlanCap5h: Number(root.setting("qwenPlanCap5h", 6000)) || 6000,
      qwenPlanCapWeek: Number(root.setting("qwenPlanCapWeek", 45000)) || 45000,
      qwenPlanCapMonth: Number(root.setting("qwenPlanCapMonth", 90000)) || 90000
    })
  }

  function persistSetting(key, value) {
    root.close()
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true; repeat: true
    onTriggered: root.refresh()
  }

  IpcHandler {
    enabled: !root.manageIpc && root.ipcTarget !== ""
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
  }

  Process {
    id: engine
    command: ["python3", root.scriptPath(), "--settings", root.settingsJson()]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var output = text || ""
        try {
          var data = JSON.parse(output)
          root.providers = data.providers || []
          root.local = data.local || {}
          // no-key/no-token/no-local-store/no-local-usage are expected
          // states for unconfigured providers; only real fetch failures
          // get the banner.
          var benign = ["no-key", "no-token", "no-local-store", "no-local-usage"]
          var real = (data.errors || []).filter(function (e) { return benign.indexOf(e) < 0 })
          root.errorText = real.length ? "some providers failed" : ""
        } catch (e) {
          root.errorText = "parse failed"
        }
        root.loading = false
      }
    }
    stderr: StdioCollector { id: engineErr; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0) { root.errorText = "engine exit " + code; root.loading = false }
    }
  }

  // ---- in-panel credential store (no file editing) ------------------------- //
  property string keyAddPending: ""
  property string keyAddValue: ""
  property string keyAddStatus: ""
  property int keyAddStatusTab: 0
  function addKey(envName, value) {
    if (keyAdder.running || !envName || !value) return
    root.keyAddPending = envName
    root.keyAddValue = value
    root.keyAddStatus = ""
    keyAdder.running = true
  }
  Process {
    id: keyAdder
    // The secret goes through stdin only — argv would be world-readable in ps.
    stdinEnabled: true
    command: ["python3", root.scriptPath(), "--add-key", root.keyAddPending]
    stdout: StdioCollector { id: keyAddOut; waitForEnd: true }
    onRunningChanged: if (running) write(root.keyAddValue + "\n")
    onExited: function (code) {
      var msg = "failed"
      try {
        var r = JSON.parse(keyAddOut.text || "{}")
        msg = r.ok ? "saved " + root.keyAddPending : (r.error || "failed")
      } catch (e) { /* keep "failed" */ }
      root.keyAddValue = ""
      root.keyAddStatus = msg
      root.keyAddStatusTab += 1
      root.refresh()
    }
  }
  Timer {
    interval: 6000
    running: root.keyAddStatus !== ""
    onTriggered: root.keyAddStatus = ""
  }

  // ---- bar button ---------------------------------------------------------- //
  WidgetButton {
    id: button
    anchors.fill: parent
    visible: !root.barShowsData
    bar: root.bar
    text: "AI"
    tooltipText: root.compactChip(root.defaultRecord())
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
      else root.refresh()
    }
  }

  // Data-mode bar chip: a compact text line for the default provider.
  WidgetButton {
    id: dataButton
    anchors.fill: parent
    visible: root.barShowsData
    bar: root.bar
    text: root.compactChip(root.defaultRecord())
    tooltipText: "AI Usage Global — click for the panel"
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
      else root.refresh()
    }
  }

  onOpenedChanged: if (opened) {
    root.nowMs = Date.now()
    if (root.loading) root.refresh()
    Qt.callLater(function () { catcher.forceActiveFocus() })
  }

  // ---- panel --------------------------------------------------------------- //
  KeyboardPanel {
    id: panel
    anchorItem: root.barShowsData ? dataButton : button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: catcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(bodyColumn.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: catcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function (direction) {
        // Tab walks providers within the subscriptions tab.
        var n = root.shownProviders.length
        if (n > 0) root.selectedProvider = (root.selectedProvider + direction + n) % n
      }
      onTextKey: function (t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "1") root.viewTab = 0
        else if (t === "2") root.viewTab = 1
      }

      Column {
        id: bodyColumn
        width: parent.width
        spacing: Style.space(12)
        padding: Style.space(12)

        // Hero --------------------------------------------------------------- //
        Row {
          width: parent.width - Style.space(24)
          spacing: Style.space(10)
          Column {
            id: heroColumn
            spacing: 2
            Text {
              text: "AI Usage Global"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              text: root.loading ? "Scanning…" :
                    root.configuredProviders.length + " providers · " +
                    (root.local.weekTokens ? root.humanTokens(root.local.weekTokens) + " tok/wk" : "no local data")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
          Item { width: Math.max(0, parent.width - heroColumn.implicitWidth - errorText.implicitWidth - parent.spacing * 2); height: 1 }
          Text {
            id: errorText
            anchors.verticalCenter: parent.verticalCenter
            text: root.errorText
            color: root.urgent
            visible: root.errorText !== ""
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // Tab strip ---------------------------------------------------------- //
        Row {
          spacing: Style.space(6)
          Repeater {
            model: ["Subscriptions", "Consumption"]
            delegate: Rectangle {
              required property int index
              required property var modelData
              width: tabLabel.implicitWidth + Style.space(16)
              height: tabLabel.implicitHeight + Style.space(8)
              radius: height / 2
              color: active ? root.alpha(root.accent, 0.22) : root.alpha(root.foreground, 0.06)
              border.color: active ? root.accent : "transparent"
              border.width: active ? 1 : 0
              Text {
                id: tabLabel
                anchors.centerIn: parent
                text: modelData
                color: active ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: active
              }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.viewTab = index
              }
            }
          }
        }

        // ==== Tab 0: Subscriptions ========================================== //
        Column {
          visible: root.viewTab === 0
          width: parent.width - Style.space(24)
          spacing: Style.space(10)

          Repeater {
            model: root.shownProviders
            delegate: ProviderBlock {}
          }

          Text {
            visible: root.shownProviders.length === 0
            text: "No providers configured yet — paste an API key in any row below."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            width: parent.width
          }
        }

        // ==== Tab 1: Consumption ============================================ //
        Column {
          visible: root.viewTab === 1
          width: parent.width - Style.space(24)
          spacing: Style.space(10)

          // Today / week / all totals
          Row {
            width: parent.width
            spacing: Style.space(8)
            Repeater {
              model: [
                { label: "Today", value: root.humanTokens(root.local.todayTokens || 0) },
                { label: "Week", value: root.humanTokens(root.local.weekTokens || 0) },
                { label: "All", value: root.humanTokens(root.local.totalTokens || 0) }
              ]
              delegate: Rectangle {
                required property var modelData
                width: (parent.width - Style.space(16)) / 3
                height: statCol.implicitHeight + Style.space(14)
                radius: Style.cornerRadius
                color: root.alpha(root.foreground, 0.05)
                Column {
                  id: statCol
                  anchors.centerIn: parent
                  spacing: 2
                  Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: modelData.value
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                  }
                  Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: modelData.label
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
          }

          // 7-day sparkline
          Rectangle {
            width: parent.width
            height: Style.space(56)
            radius: Style.cornerRadius
            color: root.alpha(root.foreground, 0.04)
            Canvas {
              id: spark
              anchors.fill: parent
              anchors.margins: Style.space(6)
              property var series: root.local.recentDays || []
              onSeriesChanged: requestPaint()
              onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                var s = series
                if (!s || s.length === 0) return
                var maxTok = 1
                for (var i = 0; i < s.length; i++) maxTok = Math.max(maxTok, s[i].tokens || 0)
                var step = width / s.length
                var bw = step * 0.62
                for (var k = 0; k < s.length; k++) {
                  var frac = (s[k].tokens || 0) / maxTok
                  var bh = Math.max(2, frac * (height))
                  ctx.fillStyle = Qt.alpha(k === s.length - 1 ? accent : foreground, k === s.length - 1 ? 0.95 : 0.55)
                  ctx.fillRect(k * step + (step - bw) / 2, height - bh, bw, bh)
                }
              }
              readonly property color accent: root.accent
              readonly property color foreground: root.foreground
            }
          }

          // Top models by tokens
          Text {
            text: "Models"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
          Column {
            width: parent.width
            spacing: Style.space(4)
            Repeater {
              model: root.topModels()
              delegate: Row {
                width: parent.width
                spacing: Style.space(8)
                required property var modelData
                property var row: modelData
                Text {
                  width: parent.width * 0.42
                  text: row.name
                  elide: Text.ElideMiddle
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Rectangle {
                  width: parent.width * 0.36
                  height: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  radius: height / 2
                  color: root.alpha(root.foreground, 0.08)
                  Rectangle {
                    width: parent.width * row.share
                    height: parent.height
                    radius: parent.radius
                    color: root.tone(row.share)
                  }
                }
                Text {
                  text: root.humanTokens(row.tokens)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
            Text {
              visible: root.topModels().length === 0
              text: "No local token data found."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // Sources line
          Text {
            width: parent.width
            text: (root.local.sources || []).length > 0
              ? "Sources: " + (root.local.sources || []).map(function (s) {
                  return s.source + " " + root.humanTokens(s.requests) + "r"
                }).join("  ·  ")
              : ""
            visible: text !== ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }

        // Footer ------------------------------------------------------------- //
        Row {
          width: parent.width - Style.space(24)
          spacing: Style.space(8)
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: {
              var wins = (root.selected && root.selected.windows) || []
              var soonest = -1
              for (var i = 0; i < wins.length; i++) {
                var r = root.resetRemainingMs(wins[i].resetsAt)
                if (r > 0 && (soonest < 0 || r < soonest)) soonest = r
              }
              return soonest > 0 ? "next reset " + root.formatReset(soonest) : "R refresh · Tab switch"
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  // Top-N models by total tokens for the consumption tab.
  function topModels() {
    var m = (root.local && root.local.models) || ({})
    var rows = []
    var maxTok = 1
    for (var name in m) {
      var b = m[name]
      var tok = (b.inputTokens || 0) + (b.outputTokens || 0) + (b.cacheReadTokens || 0)
              + (b.cacheWriteTokens || 0) + (b.reasoningTokens || 0)
      rows.push({ name: name, tokens: tok })
      maxTok = Math.max(maxTok, tok)
    }
    rows.sort(function (a, b) { return b.tokens - a.tokens })
    var top = rows.slice(0, 8)
    for (var i = 0; i < top.length; i++) top[i].share = top[i].tokens / maxTok
    return top
  }

  // One provider: name, headline, meter bars per window with reset countdown.
  component ProviderBlock: Column {
    id: block
    required property var modelData
    width: parent ? parent.width : Style.space(340)
    spacing: Style.space(4)

    readonly property var p: modelData || ({})
    readonly property bool hot: p.id === root.defaultProviderId

    Row {
      width: parent.width
      spacing: Style.space(8)
      Text {
        width: parent.width * 0.6
        text: (p.display || "") + "  " + (p.name || "")
        elide: Text.ElideRight
        color: block.hot ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: block.hot
      }
      Item { width: parent.width * 0.15; height: 1 }
      Text {
        text: {
          if (!p.configured) return "no key"
          if (p.kind === "balance" && p.value !== null && p.value !== undefined)
            return p.label
          return p.label || "—"
        }
        color: p.configured ? root.tone((p.value !== null && p.value !== undefined ? Number(p.value) : 0) / 100) : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
      }
    }

    Repeater {
      model: (block.p.windows || [])
      delegate: Row {
        id: winRow
        required property var modelData
        width: parent.width
        spacing: Style.space(8)
        visible: modelData && (modelData.percent !== null && modelData.percent !== undefined)

        readonly property double frac: root.clamp((Number(modelData.percent) || 0) / 100, 0, 1)

        Text {
          width: Style.space(28)
          text: modelData.label || ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        Rectangle {
          width: winRow.width - Style.space(28) - Style.space(120) - Style.space(16)
          height: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          radius: height / 2

          color: root.alpha(root.foreground, 0.08)
          Rectangle {
            width: parent.width * winRow.frac
            height: parent.height
            radius: parent.radius
            color: root.tone(winRow.frac)
            Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
          }
        }
        Text {
          width: Style.space(110)
          text: Math.round(modelData.percent) + "%" + (modelData.resetsAt ? "  ⟲" + root.formatReset(root.resetRemainingMs(modelData.resetsAt)) : "")
          horizontalAlignment: Text.AlignRight
          color: winRow.frac >= 0.9 ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }

    // Unconfigured provider with a storeable credential: paste-key row.
    Row {
      visible: !block.p.configured && (block.p.keyEnv || "") !== ""
      width: parent.width
      spacing: Style.space(6)
      Rectangle {
        id: keyField
        width: parent.width - saveKey.width - Style.space(12)
        height: keyInput.implicitHeight + Style.space(12)
        radius: Style.cornerRadius
        color: root.alpha(root.foreground, 0.06)
        border.color: keyInput.activeFocus ? root.accent : "transparent"
        border.width: 1
        TextInput {
          id: keyInput
          anchors.left: parent.left; anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(8); anchors.rightMargin: Style.space(8)
          clip: true
          text: ""
          echoMode: TextInput.Password
          color: root.foreground
          selectedTextColor: root.foreground
          selectionColor: root.alpha(root.accent, 0.4)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          selectByMouse: true
          // TextInput has no placeholderText (that's TextField-only).
          Text {
            visible: keyInput.text === ""
            anchors.left: parent.left
            text: "paste " + (block.p.keyEnv || "")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
          // The PanelKeyCatcher grabs focus on open; take ours back on click.
          MouseArea { anchors.fill: parent; cursorShape: Qt.IBeamCursor;
                      onClicked: keyInput.forceActiveFocus() }
          onAccepted: if (text.length > 0) {
            root.addKey(block.p.keyEnv, text); text = ""
          }
        }
      }
      Rectangle {
        id: saveKey
        width: saveKeyLabel.implicitWidth + Style.space(14)
        height: keyField.height
        radius: Style.cornerRadius
        color: keyInput.text.length > 0 ? root.accent : root.alpha(root.foreground, 0.08)
        Text {
          id: saveKeyLabel
          anchors.centerIn: parent
          text: root.keyAddPending === block.p.keyEnv && keyAdder.running
                ? "saving…" : "Save"
          color: keyInput.text.length > 0 ? "#111111" : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: if (keyInput.text.length > 0) {
            root.addKey(block.p.keyEnv, keyInput.text)
            keyInput.text = ""
          }
        }
      }
    }
    Text {
      visible: (block.p.keyEnv || "") !== "" && root.keyAddStatus !== ""
               && (root.keyAddStatus.indexOf(block.p.keyEnv) >= 0
                   || (root.keyAddPending === block.p.keyEnv && keyAdder.running))
      text: keyAdder.running ? "" : root.keyAddStatus
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      width: parent.width
      text: block.p.detail || ""
      visible: text !== "" && block.p.configured
      elide: Text.ElideRight
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
