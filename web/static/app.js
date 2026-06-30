const { createApp } = Vue

window.__app = createApp({
  data() {
    return {
      view: 'console', profiles: [], grabs: [], meta: { impersonates: [], id_types: {}, default_impersonate: 'safari260_ios' },
      px: {},
      editing: false, origName: null, step: 0, maxStep: 0,
      draft: { name: '新配置', impersonate: 'safari260_ios', fallback_direct: true, base_interval: 300, offset: 'auto', stop_policy: { success: 'session', soldout: 'session', limit: 'daytype' }, pace_policy: { relief_ms: 120, throttle: { mode: 'auto', value: 300 }, risk: { mode: 'auto', value: 800 }, curve: 'accel', max_ms: 1500, jitter_ms: 40 } },
      pxInput: '', proxiesCount: 0,
      loginId: null, loggedIn: false, account: null, loginMsg: '正在准备登录环境…', loginCls: '', loginUrl: '', _loginPoll: null, _loginGen: 0,
      bindOk: null, bindChecking: false, bind: { name: '', id_type: 0, personal_id: '', ticket4: '' }, binding: false,
      sessions: [], selected: {}, sessQ: '', sessLoading: false,
      saving: false,
      curGrab: null, snap: null, _ws: null, _wsClosing: false, _wsRetry: 0, _lastLogLen: 0, _wonIds: {}, _snapSeeded: false,
      disp: { sent: 0, win: 0, relief: 0, risk: 0, throttle: 0, net: 0 },
      settle: null, _settleId: 0, _settleQueue: [], _settleTimers: [], _audioUnlocked: false,
      ticketProfile: '', tickets: null, ticketLoading: false,
      settings: {
        proxy_concurrency: 40, settle_enabled: true, settle_music: '',
        notify_on_win: true, notify_on_done: false, notify_on_risk: false,
        tg_enabled: false, tg_token: '', tg_chat: '', tg_proxy: '',
        webhook_enabled: false, webhook_url: '',
        smtp_enabled: false, smtp_host: '', smtp_port: 465, smtp_user: '', smtp_pass: '', smtp_to: '',
      },
      musicList: [],
      toasts: [], _tid: 0,
      STEPS: ['模拟设备', '代理', '登录', '票号绑定', '选择场次', '抢票设置'],
    }
  },
  computed: {
    runningGrabs() { return this.grabs.filter(g => !g.done) },
    pxPlaceholder() { return this.editing && this.proxiesCount ? `已有 ${this.proxiesCount} 个 · 留空不改 · 填 None 清空` : '留空 = 直连' },
    pxHint() {
      const raw = this.pxInput.trim()
      if (!raw) return this.editing && this.proxiesCount ? `已有 ${this.proxiesCount} 个代理 · 留空不改,填 None 清空` : '未填 = 直连'
      if (this._isClear(raw)) return '将清空代理(直连)'
      return `将设为 ${raw.split(/[,，\s]+/).filter(Boolean).length} 项`
    },
    bindMsg() {
      if (this.bindChecking) return '检查绑定状态…'
      if (this.bindOk === true) return '已绑定门票实名信息(→ 继续)'
      if (this.bindOk === false) return '尚未绑定,填写实名信息后提交:'
      return '绑定状态查询失败,可继续(若抢票提示未绑定再回来绑)'
    },
    selCount() { return Object.keys(this.selected).filter(k => this.selected[k]).length },
    filteredSessions() {
      const q = this.sessQ.trim().toLowerCase()
      return q ? this.sessions.filter(o => (o.title + o.date + (o.location || '')).toLowerCase().includes(q)) : this.sessions
    },
    navHint() {
      if (this.step === 2) return '用「哔哩哔哩」App 扫码;扫不出可复制链接。已登录可「重新登录」换号。'
      if (this.step === 4) return '点行勾选 · 「全选可抢」 · 顶部可搜索'
      return this.editing ? '点左侧步骤可任意跳转' : '按步骤逐步完成'
    },
    wonCount() { return this.snap ? this.snap.rows.filter(r => r.ok).length : 0 },
    monState() {
      const s = this.snap
      if (!s) return { text: '连接中', cls: '' }
      if (s.state === 'done') return { text: '已结束', cls: 'done' }
      if (s.countdown_ms > 0) return { text: '蹲点中', cls: 'wait' }
      return { text: '抢票中', cls: 'hot' }
    },
  },
  methods: {
    async api(url, opts) { try { const r = await fetch(url, opts); return await r.json() } catch (e) { return null } },
    go(v) { this.view = v },
    _defStopPolicy() { return { success: 'session', soldout: 'session', limit: 'daytype' } },
    _defPacePolicy() { return { relief_ms: 120, throttle: { mode: 'auto', value: 300 }, risk: { mode: 'auto', value: 800 }, curve: 'accel', max_ms: 1500, jitter_ms: 40 } },
    async loadMeta() { this.meta = await this.api('/api/meta') || this.meta; if (!this.draft.impersonate) this.draft.impersonate = this.meta.default_impersonate },
    async loadProfiles() { this.profiles = await this.api('/api/profiles') || [] },
    async refreshGrabs() { this.grabs = await this.api('/api/grab') || [] },
    async delProfile(name) { if (!confirm(`删除配置「${name}」?`)) return; await this.api(`/api/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' }); this.toast(`已删除「${name}」`, 'ok'); this.loadProfiles() },

    newProfile() {
      this.editing = false; this.origName = null; this.loginId = null; this.loggedIn = false; this.account = null
      this.bindOk = null; this.bindChecking = false; this.sessions = []; this.selected = {}; this.pxInput = ''; this.proxiesCount = 0; this.maxStep = 0
      this.draft = { name: '新配置', impersonate: this.meta.default_impersonate, fallback_direct: true, base_interval: 300, offset: 'auto', stop_policy: this._defStopPolicy(), pace_policy: this._defPacePolicy() }
      this.loginMsg = '正在准备登录环境…'; this.loginCls = ''; this.loginUrl = ''
      this.go('wizard'); this.goStep(0)
    },
    async editProfile(name) {
      const p = await this.api(`/api/profiles/${encodeURIComponent(name)}`); if (!p || p.error) return
      this.editing = true; this.origName = name; this.loginId = null
      this.draft = { name: p.name, impersonate: p.impersonate, fallback_direct: p.fallback_direct, base_interval: p.base_interval, offset: p.offset, stop_policy: p.stop_policy || this._defStopPolicy(), pace_policy: p.pace_policy || this._defPacePolicy() }
      this.pxInput = ''; this.proxiesCount = p.proxies; this.selected = {}; this.sessions = []
      this.loggedIn = false; this.account = null; this.bindOk = null; this.maxStep = 5
      this.go('wizard'); this.goStep(0)
      const acc = await this.api(`/api/profiles/${encodeURIComponent(name)}/account`)
      if (acc && acc.info) { this.loggedIn = true; this.account = acc.info; if (this.step === 2) this.showAccount() }
      const b = await this.api(`/api/profiles/${encodeURIComponent(name)}/bound`)
      if (b && !b.error) this.bindOk = b.bound === true ? true : (b.bound === false ? false : null)
    },
    reached(i) {
      if (i === 2) return this.loggedIn
      if (i === 3) return this.bindOk === true
      if (i >= 4) return this.bindOk !== false && i < this.maxStep
      return i < this.maxStep
    },
    canEnter(i) { if (i >= 3 && !this.loggedIn) return '需先在「登录」完成登录'; if (i >= 4 && this.bindOk === false) return '需先完成「票号绑定」'; return null },
    goStep(i) { this.step = i; this.maxStep = Math.max(this.maxStep, i); this.enterStep(i) },
    jumpStep(i) {
      if (i === this.step) return
      const block = this.canEnter(i); if (block) return this.toast(block, 'warn')
      if (!this.editing && i > this.maxStep) return this.toast('请按步骤逐步完成', 'warn')
      this.goStep(i)
    },
    prevStep() { if (this.step > 0) this.goStep(this.step - 1) },
    nextStep() {
      if (this.step === 5) return this.saveProfile()
      const block = this.canEnter(this.step + 1); if (block) return this.toast(block, 'warn')
      this.goStep(this.step + 1)
    },
    enterStep(i) {
      if (i === 2) { if (this.loggedIn) this.showAccount(); else this.startLogin() }
      else if (i === 3) { if (this.bindOk !== true) this.loadBound() }
      else if (i === 4) { if (!this.sessions.length) this.loadSessions() }
    },

    showAccount() {
      const a = this.account || {}; this.loginUrl = ''; this.loginCls = 'ok'
      this.loginMsg = `当前账号:${a.uname}(uid ${a.uid} · Lv.${a.level || 0}${a.is_vip ? ' · 大会员' : ''})\n换号请点「重新登录 / 切换账号」`
      this.renderQR('')
    },
    async startLogin() {
      const gen = ++this._loginGen
      this.loginCls = ''; this.loginMsg = '正在生成二维码…'; this.loginUrl = ''; this.renderQR('')
      const r = await this.api('/api/login/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ impersonate: this.draft.impersonate }) })
      if (gen !== this._loginGen) return
      if (!r || r.error) { this.loginMsg = (r && r.error) || '登录启动失败'; this.loginCls = 'err'; return }
      this.loginId = r.id; this.loginUrl = r.url; this.renderQR(r.url); this.loginMsg = '用「哔哩哔哩」App 扫码并确认'; this.loginCls = ''
      this.pollLogin(gen)
    },
    pollLogin(gen) {
      if (this._loginPoll) clearTimeout(this._loginPoll)
      const tick = async () => {
        if (gen !== this._loginGen || this.step !== 2 || !this.loginId) return
        const s = await this.api(`/api/login/${this.loginId}`)
        if (gen !== this._loginGen) return
        if (!s) { this._loginPoll = setTimeout(tick, 1500); return }
        const M = { waiting: '等待扫码…', scanned: '已扫描,请在手机点击确认…', expired: '二维码已失效,点「重新登录」', error: '登录失败,点「重新登录」', timeout: '二维码超时,点「重新登录」' }
        if (s.state === 'success') { this.loggedIn = true; this.account = s.info; this.showAccount(); this.toast(`登录成功 ${s.info.uname}`, 'ok'); this.maxStep = Math.max(this.maxStep, 2); return }
        this.loginMsg = M[s.state] || s.state; this.loginCls = ['expired', 'error', 'timeout'].includes(s.state) ? 'err' : ''
        if (['expired', 'error', 'timeout', 'gone'].includes(s.state)) return
        this._loginPoll = setTimeout(tick, 1500)
      }
      tick()
    },
    relogin() { this.loggedIn = false; this.loginId = null; this.account = null; this.startLogin() },
    renderQR(url) {
      this.$nextTick(() => {
        const box = document.getElementById('qrbox'); if (!box) return
        if (!url) { box.className = 'qr-ph'; box.textContent = this.loggedIn ? '已登录' : '准备中…'; return }
        try { const qr = qrcode(0, 'L'); qr.addData(url); qr.make(); box.className = ''; box.innerHTML = qr.createSvgTag({ cellSize: 6, margin: 2 }) }
        catch (e) { box.textContent = '二维码渲染失败' }
      })
    },

    async loadBound() {
      this.bindChecking = true
      const u = this.loginId ? `/api/login/${this.loginId}/bound` : `/api/profiles/${encodeURIComponent(this.origName)}/bound`
      const b = await this.api(u); this.bindChecking = false
      this.bindOk = (b && !b.error) ? (b.bound === true ? true : (b.bound === false ? false : null)) : null
    },
    async submitBind() {
      this.binding = true
      const u = this.loginId ? `/api/login/${this.loginId}/bind` : `/api/profiles/${encodeURIComponent(this.origName)}/bind`
      const r = await this.api(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.bind) })
      this.binding = false
      if (r && r.ok) { this.bindOk = true; this.toast('绑定成功', 'ok') } else this.toast((r && r.message) || '绑定失败', 'err')
    },

    async loadSessions() {
      this.sessLoading = true
      const u = this.loginId ? `/api/login/${this.loginId}/sessions` : `/api/profiles/${encodeURIComponent(this.origName)}/sessions`
      const list = await this.api(u); this.sessLoading = false
      if (!Array.isArray(list)) { this.toast('拉取场次失败', 'err'); return }
      this.sessions = list
      const sel = {}; (await this._existingReserveIds()).forEach(id => sel[id] = true); this.selected = sel
    },
    async _existingReserveIds() {
      if (!this.editing || !this.origName) return []
      const p = await this.api(`/api/profiles/${encodeURIComponent(this.origName)}`)
      return (p && p.session_list || []).map(s => s.reserve_id)
    },
    isSel(id) { return !!this.selected[id] },
    toggleSession(o) { if (!o.ticket_no) return; this.selected[o.reserve_id] = !this.selected[o.reserve_id] },
    selectAllSessions() { const s = {}; this.sessions.forEach(o => { if (o.ticket_no) s[o.reserve_id] = true }); this.selected = s; this.toast(`已全选 ${Object.keys(s).length} 个`, 'ok') },

    _isClear(raw) { return ['-', '清空', '无', 'none', 'null'].includes((raw || '').trim().toLowerCase()) },
    proxiesPayload() {
      const raw = this.pxInput.trim()
      if (this.editing && !raw) return null
      if (this._isClear(raw)) return []
      return raw.split(/[,，\s]+/).filter(Boolean)
    },
    async saveProfile() {
      this.saving = true
      const selOpts = this.sessions.filter(o => this.selected[o.reserve_id] && o.ticket_no)
      const body = {
        name: this.draft.name, impersonate: this.draft.impersonate, fallback_direct: this.draft.fallback_direct,
        base_interval: this.draft.base_interval, offset: this.draft.offset, proxies: this.proxiesPayload(),
        stop_policy: this.draft.stop_policy, pace_policy: this.draft.pace_policy,
        sessions: (this.editing && !this.sessions.length) ? null : selOpts, login_id: this.loginId, orig_name: this.origName,
      }
      const r = await this.api('/api/profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      this.saving = false
      if (!r || r.error) return this.toast((r && r.error) || '保存失败', 'err')
      this.toast(`${this.editing ? '已更新' : '已创建'}配置「${r.name}」${r.renamed ? '(自动改名)' : ''}`, 'ok')
      await this.loadProfiles(); this.go('console')
    },

    async proxyCheck(name) {
      const r = await this.api(`/api/profiles/${encodeURIComponent(name)}/proxy-check`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ concurrency: this.settings.proxy_concurrency }) })
      if (!r || r.error) return this.toast((r && r.error) || '检测失败', 'warn')
      this.toast(`开始检测「${name}」${r.total} 个代理`, 'info'); this.px[name] = `检测中 0/${r.total}…`
      const poll = async () => {
        const s = await this.api(`/api/proxy/${r.id}`); if (!s) return
        if (s.state === 'running') { this.px[name] = `检测中 ${s.done}/${s.total}…`; setTimeout(poll, 500) }
        else if (s.state === 'done') { this.px[name] = `可用 ${s.available}/${s.total},已过滤保存`; this.toast(`「${name}」可用 ${s.available}/${s.total}`, 'ok'); this.loadProfiles() }
        else this.px[name] = '检测失败'
      }
      setTimeout(poll, 500)
    },

    async startGrab(names) {
      const r = await this.api('/api/grab/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profiles: names }) })
      if (!r || r.error) return this.toast(((r && r.error) || '开抢失败') + ((r && r.skipped && r.skipped.length) ? ' (跳过 ' + r.skipped.join(',') + ')' : ''), 'err')
      if (r.skipped && r.skipped.length) this.toast('跳过已在抢的:' + r.skipped.join(','), 'warn')
      await this.refreshGrabs(); this.openMonitor(r.id)
    },
    openMonitor(gid) {
      this.curGrab = gid; this.snap = null; this.disp = { sent: 0, win: 0, relief: 0, risk: 0, throttle: 0, net: 0 }
      this._wonIds = {}; this._snapSeeded = false; this._lastLogLen = 0; this._wsClosing = false; this._wsRetry = 0
      this.go('monitor'); this.connectWS(gid)
    },
    connectWS(gid) {
      if (this._ws) { try { this._ws.onclose = null; this._ws.close() } catch (e) {} }
      this._wsClosing = false
      const ws = new WebSocket(`ws://${location.host}/ws/grab/${gid}`)
      ws.onmessage = (e) => {
        const s = JSON.parse(e.data)
        if (s.state === 'gone') { ws.onclose = null; ws.close(); this.snap = null; if (this.curGrab === gid) this.curGrab = null; this.refreshGrabs(); return }
        this._wsRetry = 0; this.snap = s; this.handleSnap(s)
      }
      ws.onclose = () => { if (!this._wsClosing && this.curGrab === gid && this._wsRetry < 6) { this._wsRetry++; setTimeout(() => { if (this.curGrab === gid && !this._wsClosing) this.connectWS(gid) }, Math.min(3000, 400 * this._wsRetry)) } }
      this._ws = ws
    },
    handleSnap(s) {
      const rows = s.rows || [], log = s.log || []
      if (!this._snapSeeded) {
        this._snapSeeded = true
        for (const r of rows) if (r.ok) this._wonIds[r.account + '#' + r.reserve_id] = true
        this._lastLogLen = log.length
        return
      }
      for (const r of rows) { const k = r.account + '#' + r.reserve_id; if (r.ok && !this._wonIds[k]) { this._wonIds[k] = true; this.showSettle(r) } }
      for (let i = this._lastLogLen; i < log.length; i++) {
        const l = log[i]
        if (l.includes('切换代理')) this.toast(l, 'warn')
        else if (l.includes('异常') || l.includes('失败') || l.includes('跳过')) this.toast(l, 'warn')
      }
      this._lastLogLen = log.length
    },
    showSettle(row) {
      if (!this.settings.settle_enabled) { this.toast(`抢中 ${row.account} · ${row.title}`, 'ok'); return }
      const prof = this.profiles.find(p => p.name === row.account)
      const item = { account: row.account, title: row.title, date: (row.date || '').slice(4), face: prof ? prof.face : '' }
      if (this.settle) { this._settleQueue.push(item); return }
      this._showSettleNow(item)
    },
    _showSettleNow(item) {
      const id = ++this._settleId
      this.settle = { id, ...item }
      this._playAndDismiss(id)
    },
    _pickMusic() {
      const m = this.settings.settle_music
      if (m === '__random__') { const L = this.musicList || []; return L.length ? L[Math.floor(Math.random() * L.length)] : '' }
      return m || ''
    },
    _clearSettleTimers() { (this._settleTimers || []).forEach(clearTimeout); this._settleTimers = [] },
    closeSettle() {
      this._clearSettleTimers()
      const a = document.getElementById('winaudio'); if (a) { try { a.pause(); a.onended = null } catch (e) {} }
      this.settle = null
      if (this._settleQueue.length) {
        const next = this._settleQueue.shift()
        setTimeout(() => this._showSettleNow(next), 460)
      }
    },
    _playAndDismiss(id) {
      this._clearSettleTimers()
      const music = this._pickMusic(), a = document.getElementById('winaudio')
      const done = () => { if (this.settle && this.settle.id === id) this.closeSettle() }
      if (music && this._audioUnlocked && a) {
        a.src = `/music/${encodeURIComponent(music)}`; a.volume = 0.7
        a.onended = () => done()
        a.play().catch(() => this._settleTimers.push(setTimeout(done, 6000)))
        this._settleTimers.push(setTimeout(done, 300000))
      } else {
        this._settleTimers.push(setTimeout(done, 6000))
      }
    },
    unlockAudio() {
      if (this._audioUnlocked) return
      this._audioUnlocked = true
      const a = document.getElementById('winaudio'); if (!a) return
      a.muted = true
      a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false }).catch(() => { a.muted = false })
    },
    requestAudio() {
      this._audioUnlocked = true
      const a = document.getElementById('winaudio'); if (!a) return
      if (this.musicList && this.musicList[0]) a.src = `/music/${encodeURIComponent(this.musicList[0])}`
      a.muted = true
      a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false; a.removeAttribute('src')
        this.toast('已开启声音,胜利时自动播放', 'ok') })
        .catch(() => { a.muted = false; this.toast('已记录;若胜利时无声,先点一下页面任意处即可', 'info') })
    },
    async stopGrab(gid) {
      this._wsClosing = true
      if (this._ws) { try { this._ws.onclose = null; this._ws.close() } catch (e) {} this._ws = null }
      await this.api(`/api/grab/${gid}/stop`, { method: 'POST' })
      this.snap = null; await this.refreshGrabs()
      if (!this.grabs.length) this.go('console'); else this.openMonitor(this.grabs[0].id)
    },
    phaseCls(r) { return r.ok ? 'p-win' : (['停止', '截止', '异常'].includes(r.phase) ? 'p-stop' : (['开抢', '退避', '抢票中'].includes(r.phase) ? 'p-active' : '')) },
    retLabel(r) {
      if (r.ok) return '已抢中'
      if (r.msg) return r.msg
      if (r.code === 0) return '成功'
      return r.code != null ? '返回 ' + r.code : '—'
    },
    _animTick() {
      if (!this.snap) return
      const s = this.snap.stats || {}
      for (const k of ['sent', 'win', 'relief', 'risk', 'throttle', 'net']) {
        const t = s[k] || 0, c = this.disp[k]
        if (c !== t) { const step = Math.max(1, Math.ceil(Math.abs(t - c) * 0.34)); this.disp[k] = c < t ? Math.min(t, c + step) : Math.max(t, c - step) }
      }
    },

    openTickets() { this.go('tickets'); if (!this.ticketProfile && this.profiles.length) this.ticketProfile = this.profiles[0].name; if (this.ticketProfile) this.loadTickets() },
    async loadTickets() {
      if (!this.ticketProfile) return
      this.ticketLoading = true; this.tickets = null
      const r = await this.api(`/api/profiles/${encodeURIComponent(this.ticketProfile)}/tickets`)
      this.ticketLoading = false; this.tickets = (r && r.sessions) || []
    },

    async loadSettings() { const s = await this.api('/api/settings'); if (s && !s.error) Object.assign(this.settings, s); this.musicList = await this.api('/api/music') || [] },
    async saveSettings() { const r = await this.api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.settings) }); this.toast((r && r.ok) ? '设置已保存' : '保存失败', (r && r.ok) ? 'ok' : 'err') },
    async testNotify(kind) { await this.saveSettings(); const r = await this.api('/api/notify/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) }); this.toast((r && r.ok) ? `${kind} 测试已发送` : `测试失败:${(r && r.message) || ''}`, (r && r.ok) ? 'ok' : 'err') },
    previewMusic() { const m = this._pickMusic(); if (!m) return; this.unlockAudio(); const a = document.getElementById('winaudio'); a.src = `/music/${encodeURIComponent(m)}`; a.volume = 0.7; a.play().catch(() => {}) },
    previewSettle() {
      this.unlockAudio()
      this._settleQueue = []
      const p = this.profiles[0] || {}
      this._showSettleNow({ account: p.name || '示例账号', title: 'test', date: '0721', face: p.face || '' })
    },

    toast(msg, kind = 'info') {
      const id = ++this._tid, dur = 4000
      this.toasts.push({ id, msg, kind, dur })
      while (this.toasts.length > 3) this.toasts.shift()
      setTimeout(() => { this.toasts = this.toasts.filter(x => x.id !== id) }, dur)
    },
    fmtTs(s) { if (!s) return '—'; const d = new Date(s * 1000); return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` },
    fmtDate(d) { return (d && d.length === 8) ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : (d || '—') },
    fmtAct(b, e) {
      if (!b) return '—'
      const s = this.fmtTs(b)
      if (e && new Date(b * 1000).toDateString() === new Date(e * 1000).toDateString()) {
        const d = new Date(e * 1000)
        return `${s}~${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
      }
      return s
    },
    fmtDur(ms) { let s = Math.floor(ms / 1000); const h = Math.floor(s / 3600); s %= 3600; const m = Math.floor(s / 60); s %= 60; return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0') },
  },
  mounted() { this.loadMeta(); this.loadProfiles(); this.refreshGrabs(); this.loadSettings(); setInterval(this._animTick, 70) },
}).mount('#app')
