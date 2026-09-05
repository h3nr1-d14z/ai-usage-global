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
// opens the panel; right/middle-click or 'R' refreshes; Esc closes. The 1s
// nowMs ticker keeps every reset countdown honest while open.
// Key rows: clipboard paste (wl-clipboard) + show/hide mask, Enter saves; a
// configured Qwen on the census fallback re-offers its paste row.
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
  property int viewTab: 0   // 0 = subscriptions, 1 = consumption
  // How many paste-field TextInputs hold activeFocus. PanelKeyCatcher
  // handles keys BeforeItem even for focused descendants — without this
  // gate it hijacks j/k/h/l/x/r from the field and swallows the Enter
  // that onAccepted needs ("Enter saves" only worked via the button).
  property int keyFieldsFocused: 0
  // Last observed window percents (provider:window -> percent) for the
  // opt-in cap alerts; the first observation seeds silently so shell
  // restarts never fire a stale alert.
  property var lastWindowPercents: ({})

  // Ticks once a second while the shell is alive so countdowns stay honest.
  property double nowMs: Date.now()
  Timer { interval: 1000; running: true; repeat: true; onTriggered: root.nowMs = Date.now() }

  readonly property int refreshIntervalSec: Math.max(60, Number(root.setting("refreshIntervalSec", 900)) || 900)
  readonly property string barDisplay: String(root.setting("barDisplay", "Data"))
  readonly property bool barShowsData: barDisplay.toLowerCase() === "data"
  readonly property string defaultProviderId: String(root.setting("defaultProvider", "opencode"))
  // Paste rows for unconfigured providers sit behind the "+ add provider"
  // affordance, so the panel leads with usage, not credential forms.
  // Auto-expanded only while nothing is configured; the first user toggle
  // takes over the property for good.
  property bool showAddRows: root.configuredProviders.length === 0
  readonly property bool hasAddRows: {
    var list = root.providers || []
    for (var i = 0; i < list.length; i++)
      if (list[i] && !list[i].configured && (list[i].keyEnv || "") !== "") return true
    return false
  }
  readonly property var shownProviders: (root.providers || []).filter(function (p) {
    return p && (p.configured || (root.showAddRows && (p.keyEnv || "") !== ""))
  })
  readonly property var configuredProviders: (root.providers || []).filter(function (p) { return p && p.configured })


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

  // Compact bar chip: provider code + its hottest window + %. Color carries
  // the tone (dataButton.activeColor); hover shows the full detail line.
  function compactChip(p) {
    if (!p) return "AI —"
    if (p.kind === "balance" && p.label) return (p.display || p.name) + " " + p.label
    var wins = (p.windows || []).filter(function (w) { return w && w.percent !== null })
    if (wins.length === 0) return (p.display || "") + " —"
    var hot = wins[0]
    for (var i = 1; i < wins.length; i++)
      if ((wins[i].percent || 0) > (hot.percent || 0)) hot = wins[i]
    return (p.display || "") + " " + hot.label + " " + Math.round(hot.percent) + "%"
  }

  // Hover detail for the chip: one short line per configured provider —
  // name, hottest window, its reset. The chip itself shows only the
  // default provider; full per-window detail lives in the panel.
  function chipDetail() {
    var def = root.defaultRecord()
    var list = root.configuredProviders.filter(function (p) { return p !== def })
    if (def) list.unshift(def)
    if (list.length === 0) return "AI Usage Global — no data yet"
    var lines = []
    for (var i = 0; i < list.length; i++) {
      var p = list[i]
      if (p.kind === "balance" && p.label) {
        lines.push((p.name || "") + "  " + p.label)
        continue
      }
      var wins = (p.windows || []).filter(function (w) { return w && w.percent !== null })
      var hot = null
      for (var j = 0; j < wins.length; j++)
        if (!hot || (wins[j].percent || 0) > (hot.percent || 0)) hot = wins[j]
      if (!hot) { lines.push(p.name || ""); continue }
      var r = root.resetRemainingMs(hot.resetsAt)
      lines.push((p.name || "") + "  " + hot.label + " " + Math.round(hot.percent) + "%"
                 + (r > 0 ? " · " + root.formatReset(r) : ""))
    }
    // The live bar flattened "\n" (cause unverified — isolated tests with
    // the exact string and structure break fine). "<br>" renders as a
    // break under both plain AutoText and rich-text mode, so it is the
    // robust separator here. Names/labels contain no < > &.
    return lines.join("<br>")
  }

  // Console cookie missing/stale: fetch_qwen fell back to the local census.
  // True only for a configured qwen whose detail is census-flavoured.
  function onCensusCookie(p) {
    return p && p.id === "qwen" && p.configured
      && String(p.detail || "").indexOf("local") === 0
  }

  // One dim hint line under a paste-key row: where the credential lives.
  function keyHint(p) {
    if (!p) return ""
    if (root.onCensusCookie(p))
      return "showing local counts — paste a fresh console cookie for live 5h/7d %"
    var hints = {
      opencode: "key: OpenCode Zen Go plan key",
      openrouter: "key: openrouter.ai/keys",
      kimi: "key: platform.moonshot.ai → API Keys",
      zai: "key: z.ai console → API Keys",
      deepseek: "key: platform.deepseek.com → API Keys",
      copilot: "token: github.com → Developer settings → Personal access tokens",
      qwen: "cookie: QwenCloud → DevTools → any request → copy the cookie: header",
      trollllm: "cookie: trollllm.xyz → DevTools → any /api/user request → cookie: header"
    }
    return hints[p.id] || ""
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
    // No close(): the clock panel proves in-place entry updates survive
    // updateEntryInline; closing here made every toggle feel like a crash.
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  // Opt-in desktop alert when a metered window crosses 90% (low -> high
  // only; disabled resets the baseline so re-enabling starts fresh).
  function checkCapAlerts(providers) {
    if (String(root.setting("capAlerts", "Off")).toLowerCase() !== "on") {
      root.lastWindowPercents = ({})
      return
    }
    var firing = null
    for (var i = 0; i < providers.length; i++) {
      var p = providers[i]
      if (!p.configured) continue
      var windows = p.windows || []
      for (var j = 0; j < windows.length; j++) {
        var w = windows[j]
        var pct = w.percent
        if (pct === null || pct === undefined) continue
        var key = p.id + ":" + (w.id || w.label)
        var prev = root.lastWindowPercents[key]
        root.lastWindowPercents[key] = pct
        if (prev !== undefined && prev < 90 && pct >= 90) {
          // One notification per refresh (highest crossing) — a Quickshell
          // Process cannot queue; back-to-back command swaps would drop all
          // but the last.
          if (!firing || pct > firing.pct)
            firing = { pct: pct, name: p.name, label: w.label || w.id,
                       resets: w.resetsAt || "?" }
        }
      }
    }
    if (firing) {
      notifier.command = ["notify-send", "-a", "AI Usage", "-u", "normal",
                          firing.name + " · " + firing.label,
                          firing.pct + "% used · resets " + firing.resets]
      notifier.running = true
    }
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
          root.checkCapAlerts(data.providers || [])
          root.local = data.local || {}
          // no-key/no-token/no-cookie/no-local-store/no-local-usage are
          // expected states for unconfigured providers; only real fetch
          // failures get the banner.
          var benign = ["no-key", "no-token", "no-cookie", "no-local-store",
                        "no-local-usage"]
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

  Process {
    id: notifier
    command: ["true"]
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
    tooltipText: root.chipDetail()
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
      else root.refresh()
    }
  }

  // Data-mode bar chip: short text, tone-colored, detail on hover.
  WidgetButton {
    id: dataButton
    anchors.fill: parent
    visible: root.barShowsData
    bar: root.bar
    text: root.compactChip(root.defaultRecord())
    tooltipText: root.chipDetail()
    active: true
    activeColor: {
      var p = root.defaultRecord()
      if (!p || p.kind !== "percent") return dataButton.foreground
      var v = Number(p.value)
      if (v >= 90) return root.urgent
      if (v >= 70) return root.accent
      return dataButton.foreground
    }
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
    contentHeight: panel.fittedContentHeight(bodyColumn.implicitHeight)

    PanelKeyCatcher {
      id: catcher
      anchors.fill: parent
      blocked: root.keyFieldsFocused > 0
      onCloseRequested: root.close()
      onTabRequested: function (direction) {
        // Swallow Tab so it neither closes the panel nor falls through;
        // the invisible provider-selection walk was removed.
      }
      onTextKey: function (t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "a" || t === "A") root.showAddRows = !root.showAddRows
        else if (t === "1") root.viewTab = 0
        else if (t === "2") root.viewTab = 1
        else if (t === "3") root.viewTab = 2
      }

      // Scrollable once content outgrows the capped card (many providers,
      // small screens); a no-op at equal heights.
      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: bodyColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: bodyColumn
          width: scroller.width
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
            model: ["Subscriptions", "Consumption", "Settings"]
            delegate: Rectangle {
              required property int index
              required property var modelData
              // Explicit: an unqualified `active` resolves to the window's
              // focus state, painting BOTH tabs as active — the strip never
              // actually showed which tab is selected.
              readonly property bool active: root.viewTab === index
              width: tabLabel.implicitWidth + Style.space(16)
              height: tabLabel.implicitHeight + Style.space(8)
              radius: height / 2
              color: active ? root.alpha(root.accent, 0.32)
                            : (tabMouse.containsMouse ? root.alpha(root.foreground, 0.12)
                                                      : root.alpha(root.foreground, 0.06))
              border.color: active ? root.accent : "transparent"
              border.width: active ? 1 : 0
              Text {
                id: tabLabel
                anchors.centerIn: parent
                text: modelData
                color: active || tabMouse.containsMouse ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: active
              }
              MouseArea {
                id: tabMouse
                anchors.fill: parent
                hoverEnabled: true
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
          spacing: Style.space(14)

          Repeater {
            model: root.shownProviders
            delegate: ProviderBlock {}
          }

          // Paste rows for unconfigured providers sit behind this toggle so
          // the panel leads with usage, not credential forms.
          Rectangle {
            visible: root.hasAddRows
            width: parent.width
            height: addRowLabel.implicitHeight + Style.space(10)
            radius: Style.cornerRadius
            color: addPillMouse.containsMouse ? root.alpha(root.foreground, 0.09)
                                             : root.alpha(root.foreground, 0.04)
            Text {
              id: addRowLabel
              anchors.centerIn: parent
              text: root.showAddRows ? "− hide" : "+ add provider"
              color: addPillMouse.containsMouse ? root.foreground : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            MouseArea {
              id: addPillMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.showAddRows = !root.showAddRows
            }
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

          // 7-day sparkline with weekday labels (last bar = today, accent)
          Column {
            width: parent.width
            spacing: 2
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
                    ctx.fillStyle = Qt.alpha(k === s.length - 1 ? accent : foreground,
                                             k === s.length - 1 ? 0.95 : 0.55)
                    ctx.fillRect(k * step + (step - bw) / 2, height - bh, bw, bh)
                  }
                }
                readonly property color accent: root.accent
                readonly property color foreground: root.foreground
              }
            }
            Row {
              width: parent.width
              Repeater {
                id: dayLabels
                model: root.recentDayLabels()
                delegate: Text {
                  required property int index
                  required property var modelData
                  width: parent.width / Math.max(1, dayLabels.count)
                  horizontalAlignment: Text.AlignHCenter
                  text: modelData
                  color: index === dayLabels.count - 1 ? root.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }

          // 30-day local trend from persisted daily history (refresh-time
          // snapshots — survives transcript compaction/rotation).
          Column {
            width: parent.width
            spacing: 2
            Rectangle {
              width: parent.width
              height: Style.space(40)
              radius: Style.cornerRadius
              color: root.alpha(root.foreground, 0.04)
              Canvas {
                id: trend
                anchors.fill: parent
                anchors.margins: Style.space(6)
                property var series: (root.local && root.local.history) || []
                onSeriesChanged: requestPaint()
                onPaint: {
                  var ctx = getContext("2d")
                  ctx.clearRect(0, 0, width, height)
                  var s = series
                  if (!s || s.length === 0) return
                  var maxTok = 1
                  for (var i = 0; i < s.length; i++) maxTok = Math.max(maxTok, s[i].tokens || 0)
                  var step = width / s.length
                  var bw = Math.max(1, step * 0.62)
                  for (var k = 0; k < s.length; k++) {
                    var bh = Math.max(1, (s[k].tokens || 0) / maxTok * height)
                    ctx.fillStyle = Qt.alpha(k === s.length - 1 ? accent : foreground,
                                             k === s.length - 1 ? 0.95 : 0.45)
                    ctx.fillRect(k * step + (step - bw) / 2, height - bh, bw, bh)
                  }
                }
                readonly property color accent: root.accent
                readonly property color foreground: root.foreground
              }
            }
            Text {
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: "30d trend · " + root.humanTokens(
                      ((root.local && root.local.history) || [])
                        .reduce(function (a, d) { return a + (d.tokens || 0) }, 0))
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
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
                required property int index
                required property var modelData
                property var row: modelData
                Text {
                  width: parent.width * 0.46
                  text: row.name
                  elide: Text.ElideMiddle
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Rectangle {
                  width: parent.width * 0.30
                  height: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  radius: height / 2
                  color: root.alpha(root.foreground, 0.08)
                  Rectangle {
                    width: Math.max(2, parent.width * row.share)
                    height: parent.height
                    radius: parent.radius
                    color: index === 0 ? root.accent : root.alpha(root.foreground, 0.45)
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

          // OMP lane split: main agents vs spawned subagents vs the advisor,
          // plus recorded role calls (model_usage records — auto-thinking
          // probes). Explicit completion() oneshots still write no record;
          // that residual gap is flagged, not folded into a lane.
          Text {
            width: parent.width
            visible: root.ompLanes() !== null
            text: {
              var l = root.ompLanes()
              if (!l) return ""
              var names = [["main", "main"], ["subagent", "subagents"],
                           ["advisor", "advisor"], ["roles", "roles"]]
              var parts = []
              for (var i = 0; i < names.length; i++) {
                var lane = l[names[i][0]]
                if (lane && lane.tokens > 0)
                  parts.push(names[i][1] + " " + root.humanTokens(lane.tokens))
              }
              if (parts.length > 0) parts.push("oneshots unlogged")
              return "Lanes: " + parts.join(" · ")
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
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

        // ==== Tab 2: Settings ============================================== //
        Column {
          visible: root.viewTab === 2
          width: parent.width - Style.space(24)
          spacing: Style.space(14)

          Text {
            width: parent.width
            text: "Settings persist to shell.json. Cap alerts notify on " +
                  "desktop when a window crosses 90% — off by default."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          SettingRow {
            label: "Refresh every"
            SettingPill {
              value: root.refreshIntervalSec >= 3600
                     ? (root.refreshIntervalSec / 3600) + "h"
                     : (root.refreshIntervalSec / 60) + "m"
              onClicked: {
                var steps = [60, 300, 900, 3600]
                var i = steps.indexOf(root.refreshIntervalSec)
                root.persistSetting("refreshIntervalSec",
                                    steps[(i + 1 + steps.length) % steps.length])
              }
            }
          }

          SettingRow {
            label: "Default provider"
            SettingPill {
              value: {
                var list = root.configuredProviders
                if (list.length === 0) return "—"
                var cur = root.providerById(root.defaultProviderId)
                return cur ? (cur.display || cur.id) : list[0].display || list[0].id
              }
              onClicked: {
                var list = root.configuredProviders
                if (list.length === 0) return
                var i = Math.max(0, list.findIndex(function (p) { return p.id === root.defaultProviderId }))
                root.persistSetting("defaultProvider", list[(i + 1) % list.length].id)
              }
            }
          }

          SettingRow {
            label: "Qwen census caps"
            CapField { capKey: "qwenPlanCap5h"; capDefault: 6000; fieldLabel: "5h" }
            CapField { capKey: "qwenPlanCapWeek"; capDefault: 45000; fieldLabel: "wk" }
            CapField { capKey: "qwenPlanCapMonth"; capDefault: 90000; fieldLabel: "mo" }
          }

          SettingRow {
            label: "Cap alerts ≥90%"
            SettingPill {
              value: String(root.setting("capAlerts", "Off"))
              onClicked: root.persistSetting(
                "capAlerts",
                String(root.setting("capAlerts", "Off")) === "On" ? "Off" : "On")
            }
          }

          SettingRow {
            label: "Local usage"
            SettingPill {
              value: String(root.setting("showLocalConsumption", "On"))
              onClicked: root.persistSetting(
                "showLocalConsumption",
                String(root.setting("showLocalConsumption", "On")) === "On" ? "Off" : "On")
            }
          }
        }

        // Footer ------------------------------------------------------------- //
        Text {
          width: parent.width - Style.space(24)
          text: "R refresh · A add · 1/2/3 tabs"
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
      if (tok <= 0) continue
      rows.push({ name: name, tokens: tok })
      maxTok = Math.max(maxTok, tok)
    }
    rows.sort(function (a, b) { return b.tokens - a.tokens })
    var top = rows.slice(0, 8)
    for (var i = 0; i < top.length; i++) top[i].share = top[i].tokens / maxTok
    return top
  }

  // OMP transcript lanes (main / advisor / subagent) — null when the omp
  // source is absent or carries no lane data.
  function ompLanes() {
    var srcs = (root.local && root.local.sources) || []
    for (var i = 0; i < srcs.length; i++) {
      if (srcs[i].source === "omp" && srcs[i].lanes) return srcs[i].lanes
    }
    return null
  }

  // Weekday initials under the sparkline: last entry is today.
  function recentDayLabels() {
    var n = (root.local && root.local.recentDays || []).length
    var out = []
    for (var i = 0; i < n; i++) {
      var d = new Date(root.nowMs - (n - 1 - i) * 86400000)
      out.push(["S", "M", "T", "W", "T", "F", "S"][d.getDay()])
    }
    return out
  }

  // Settings row skeleton: caption label left, controls right.
  component SettingRow: Row {
    id: settingRow
    property string label: ""
    default property alias content: controls.data
    width: parent ? parent.width : 0
    spacing: Style.space(8)
    Text {
      id: settingLabel
      anchors.verticalCenter: parent.verticalCenter
      text: settingRow.label
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
    Item {
      width: Math.max(0, parent.width - settingLabel.implicitWidth
                      - controls.implicitWidth - parent.spacing * 2)
      height: 1
    }
    Row { id: controls; spacing: Style.space(6) }
  }

  // Clickable value pill (cycle-style setting control).
  component SettingPill: Rectangle {
    id: pill
    property string value: ""
    signal clicked()
    width: pillLabel.implicitWidth + Style.space(14)
    height: pillLabel.implicitHeight + Style.space(8)
    radius: height / 2
    color: pillMouse.containsMouse ? root.alpha(root.foreground, 0.12)
                                   : root.alpha(root.foreground, 0.06)
    Text {
      id: pillLabel
      anchors.centerIn: parent
      text: pill.value
      color: pillMouse.containsMouse ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
    MouseArea {
      id: pillMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: pill.clicked()
    }
  }

  // Numeric census-cap field: label + digit input, Enter persists.
  component CapField: Row {
    id: capField
    property string capKey: ""
    property int capDefault: 0
    property string fieldLabel: ""
    spacing: Style.space(4)
    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: capField.fieldLabel
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
    Rectangle {
      width: Style.space(44)
      height: capInput.implicitHeight + Style.space(6)
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.06)
      border.color: capInput.activeFocus ? root.accent : "transparent"
      border.width: 1
      TextInput {
        id: capInput
        anchors.fill: parent
        anchors.leftMargin: Style.space(6)
        verticalAlignment: TextInput.AlignVCenter
        text: String(root.setting(capField.capKey, capField.capDefault))
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        validator: IntValidator { bottom: 1; top: 1000000 }
        inputMethodHints: Qt.ImhDigitsOnly
        clip: true
        onActiveFocusChanged: root.keyFieldsFocused += activeFocus ? 1 : -1
        Keys.onEscapePressed: capInput.focus = false
        onAccepted: {
          var n = Number(text)
          if (n > 0) root.persistSetting(capField.capKey, n)
        }
      }
    }
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
        text: p.name || ""
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
          // Metered providers: each window's % is already on its meter row,
          // so this slot aggregates instead — the soonest reset.
          var wins = p.windows || []
          if (wins.length > 1) {
            var soonest = -1
            for (var i = 0; i < wins.length; i++) {
              var r = root.resetRemainingMs(wins[i].resetsAt)
              if (r > 0 && (soonest < 0 || r < soonest)) soonest = r
            }
            if (soonest > 0) return "⟲ " + root.formatReset(soonest)
          }
          return wins.length > 0 ? "" : (p.label || "—")
        }
        color: (p.configured && p.kind === "balance")
                 ? root.tone((Number(p.value) || 0) / 100) : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: p.configured && p.kind === "balance"
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
    // Also re-offered for a configured qwen stuck on the census fallback
    // (missing or stale console cookie).
    Row {
      id: keyRow
      visible: (block.p.keyEnv || "") !== ""
               && (!block.p.configured || root.onCensusCookie(block.p))
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
          anchors.leftMargin: Style.space(8)
          anchors.rightMargin: keyActions.implicitWidth + Style.space(10)
          clip: true
          text: ""
          echoMode: TextInput.Password
          color: root.foreground
          selectedTextColor: root.foreground
          selectionColor: root.alpha(root.accent, 0.4)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          selectByMouse: true
          onActiveFocusChanged: root.keyFieldsFocused += activeFocus ? 1 : -1
          // Esc while editing drops focus first; the next Esc closes the
          // panel (catcher is blocked while a field holds focus).
          Keys.onEscapePressed: keyInput.focus = false
          // TextInput has no placeholderText (that's TextField-only).
          Text {
            visible: keyInput.text === ""
            anchors.left: parent.left
            text: block.p.keyEnv || ""
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
        // Inline field actions: clipboard paste + mask toggle.
        Row {
          id: keyActions
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.rightMargin: Style.space(6)
          spacing: Style.space(8)
          Text {
            text: "paste"
            color: clipReader.running ? root.accent
                  : (pasteMouse.containsMouse ? root.foreground : root.dim)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            MouseArea { id: pasteMouse; anchors.fill: parent; hoverEnabled: true;
                        cursorShape: Qt.PointingHandCursor;
                        onClicked: if (!clipReader.running) clipReader.running = true }
          }
          Text {
            text: keyInput.echoMode === TextInput.Password ? "show" : "hide"
            color: showMouse.containsMouse ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            MouseArea { id: showMouse; anchors.fill: parent; hoverEnabled: true;
                        cursorShape: Qt.PointingHandCursor;
                        onClicked: keyInput.echoMode =
                          (keyInput.echoMode === TextInput.Password)
                            ? TextInput.Normal : TextInput.Password }
          }
        }
      }
      Rectangle {
        id: saveKey
        width: saveKeyLabel.implicitWidth + Style.space(14)
        height: keyField.height
        radius: Style.cornerRadius
        color: keyInput.text.length > 0 ? root.accent
              : (saveMouse.containsMouse ? root.alpha(root.foreground, 0.14)
                                         : root.alpha(root.foreground, 0.08))
        Text {
          id: saveKeyLabel
          anchors.centerIn: parent
          text: root.keyAddPending === block.p.keyEnv && keyAdder.running
                ? "saving…" : "Save"
          color: keyInput.text.length > 0 ? "#111111"
                : (saveMouse.containsMouse ? root.foreground : root.dim)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
        MouseArea {
          id: saveMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: if (keyInput.text.length > 0) {
            root.addKey(block.p.keyEnv, keyInput.text)
            keyInput.text = ""
          }
        }
      }
    }

    // Clipboard source for the paste action (wl-clipboard; no-op when the
    // clipboard is empty or holds no usable first line).
    Process {
      id: clipReader
      command: ["wl-paste", "--no-newline"]
      stdout: StdioCollector {
        waitForEnd: true
        onStreamFinished: {
          var t = String(text || "").split("\n")[0].trim()
          if (t.toLowerCase().indexOf("cookie:") === 0) t = t.slice(7).trim()
          if (t.length > 0) { keyInput.text = t; keyInput.forceActiveFocus() }
        }
      }
    }

    // Where this credential comes from (or why it is being re-offered).
    Text {
      visible: keyRow.visible && root.keyHint(block.p) !== ""
      width: parent.width
      text: root.keyHint(block.p)
      wrapMode: Text.WordWrap
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
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
