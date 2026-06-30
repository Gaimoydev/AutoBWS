# BWS 2026 哔哩乐园 抢票 API 逆向笔记

> 逆向自活动页 `https://www.bilibili.com/blackboard/era/bws2026-event.html`
> 主逻辑 bundle:`activity.hdslb.com/blackboard/activity3ERAcwloghvqv400/js/index.af9660b5.js`
> 逆向日期:2026-06-27(账号未绑定状态下抓取)

## 全局常量
| 名称 | 值(正式 / pre 环境) | 说明 |
|---|---|---|
| base `s` | `//api.bilibili.com` / `//pre-api.bilibili.com` | API 域名 |
| `bid` (c) | `202601` / `202602` | 活动 id |
| `year` (l) | `202601` / `202602` | 年份参数,**所有接口都带** |
| 活动日期 `ACT_DAYS` | `20260710, 20260711, 20260712` | 7/10–7/12 |
| `reserve_type` | `0`=活动场次(field) / `1`=商品场次(goods) / `-1`=全部 | |

**csrf**:http 封装层自动从 cookie `bili_jct` 注入到每个请求(GET 进 query,POST 进 body),字段名 `csrf`。
**鉴权**:cookie(`SESSDATA` 等),`withCredentials`。Origin/Referer = `https://www.bilibili.com`。

## 1. 绑定状态检查
```
GET /x/activity/bws/online/park/ticket/check?year=202601&csrf=<bili_jct>
→ {"code":0,"data":{"is_bind": false}}     // false=未绑定
```

## 2. 实名+票号 绑定(认证)
```
POST /x/activity/bws/online/park/ticket/bind
body: user_name=<姓名> id_type=<0/1/2/3> personal_id=<证件号全号>
      ticket_no=<票号后4位> bid=202601 year=202601 csrf=<bili_jct>
```
- `id_type`:`0`=身份证 `1`=护照 `2`=港澳居民来往内地通行证 `3`=台湾居民来往内地通行证(默认 0)
- `ticket_no`:**票号后 4 位**(前端校验必须正好 4 位字符)
- 前端校验:姓名非空、证件号非空、勾选《BW隐私政策》
- 返回 `code`:
  - `0` 绑定成功
  - `75636` 票务身份信息校验不通过
  - `75642` 当前账号已经被绑定
  - `75643` 当前证件下,未查询到购票信息
  - `76645` 邀请函用户暂不支持门票认证
  - `75638` 需先绑定门票信息(未绑定时其它接口返回)

## 3. 场次列表(绑定后才有数据)— ✅ 已用真实绑定账号验证
```
GET /x/activity/bws/online/park/reserve/info?reserve_date=20260710,20260711,20260712&reserve_type=0&year=202601&csrf=<bili_jct>
→ {"code":0,"data":{ reserve_list, user_ticket_info, user_reserve_info }}
```
- 未绑定时:`{"code":75638,"message":"需先绑定门票信息…","data":{"user_reserve_info":null,"reserve_list":null}}`
- `reserve_date` 可逗号传多日,也可单日。**只返回该账号持票当日的数据**(没买某天票则该日 key 不存在)。

### `data.user_ticket_info[date]` — 该日的票(reserve/do 用完整票号)
```json
"20260711": {"sid":893377,"sku_name":"游园票","screen_name":"2026-07-11 周六","type":1,"ticket":"XXXXXXXXXXXXXX","is_vip":false}
```
- `ticket` = **完整票号**,即 reserve/do 的 `ticket_no`(每天一张,不同)。

### `data.user_reserve_info[date]` — 预约额度
```json
"20260711": {"total_count":5,"cur_count":0}    // 该日该类型上限total_count,已约cur_count
```
- **额度按 (账号 + 日期 + `reserve_type`) 计**:活动场次(type 0)每人每天 **5 场**;商品场次(type 1)每人每天 **1 件**。
- `cur_count == total_count` 时,该 (日期+类型) 桶已满,对该桶内其它场次发 reserve/do 会返回 `76647`。**不影响其它日期或另一类型**。

### `data.reserve_list[date]` — 场次数组(单个场次对象)
```json
{ "reserve_id":35140,            // ← reserve/do 的 inter_reserve_id
  "act_type":"14", "act_title":"光与夜之恋-萧逸绾指同心", "act_img":"https://...",
  "act_begin_time":1783731600,   // 入场时间(秒)
  "act_end_time":1783756800,
  "reserve_begin_time":1783139700,  // ← 开抢时间(秒),蹲点目标
  "reserve_end_time":1783260000,    // 预约截止
  "describe_info":"预约前请阅读…",
  "standard_ticket_num":300, "standard_stock":300,  // 标准票名额/剩余库存
  "vip_ticket_num":0, "vip_stock":0,
  "screen_date":20260711, "is_vip_ticket":0,
  "state":1, "online_state":0, "display_index":35140,
  "reserve_type":0,                 // 0=活动场次 1=商品场次
  "reserve_location":"5.1H馆丨《光与夜之恋》官方展位",
  "next_reserve":{"reserve_begin_time":0,"reserve_end_time":0,"is_vip_ticket":0} }
```

## 4. 抢票发包(预约场次)
```
POST /x/activity/bws/online/park/reserve/do
body: inter_reserve_id=<场次 reserve_id> ticket_no=<完整票号(来自 user_ticket_info[date].ticket)>
      year=202601 csrf=<bili_jct>
```
- 返回 `code`:
  - `0` 成功(`data.cur_reserve_count` 更新已约数)
  - `412` / `429` / `76651` / `-702` 预约火爆/限流 → **重试**
  - `75574` 场次已被抢空
  - `76647` 预约数已达上限(该 **账号+日期+类型** 当天额度满:活动 5/天、商品 1/天;同桶其它场次会照样 76647,**别的日期/类型不受影响**)
  - `76650` 操作频繁
- POST 编码:web 端 axios 默认 `application/x-www-form-urlencoded`;**去年脚本用 multipart/form-data 同样成功**(两者皆可)。
- 未发现 ptoken / gaia 验证码挑战:风控仅以 412/火爆码体现,简单重试即可(与去年一致)。

## 5. 我的预约
```
GET /x/activity/bws/online/park/myreserve?year=202601&csrf=<bili_jct>
→ {"code":0,"data":{"reserve_list":{}}}    // 空=未预约
```

## 6. 服务器时间(蹲点校准)
```
GET /x/activity/bws/online/park/server/time?year=202601&csrf=<bili_jct>
→ data.server_time   // 秒级 unix 时间戳
```
前端蹲点逻辑(`timeInit`):
- 每 30s 同步一次:`delta = server_time*1000 - Date.now()`
- 每秒 tick:`currentTime = Date.now() + delta`,对齐整秒
- 场次 `reserve_begin_time`(秒)到点即可预约 → CLI 蹲到 `reserve_begin_time - 用户设定提前/延迟量` 发 reserve/do

> 本项目实现(`timesync.py` + `bws_api.ServerClock`):**不信任本地墙钟**。用 `time.monotonic()` 计时,
> 锚定到 **NTP(ntp.aliyun.com,优先,毫秒级)+ B站 /server/time(兜底+交叉校验)**。
> 实测本机墙钟比真值快 ~212ms、B站时钟与 NTP 仅差 ~22ms,故 NTP 即可精确代表 B站开闸时刻。

## ✅ 已用真实绑定账号验证(2026-06-27)
- 绑定:`ticket/bind` form-urlencoded 提交 `{user_name:<姓名>, id_type:0, personal_id:<证件号>, ticket_no:<后4位>, bid, year, csrf}` → `{"code":0,"data":1}` 成功。
- `reserve_list[date]` 场次字段已确认(见上,含 state/库存/reserve_begin_time)。
- `user_ticket_info[date].ticket` 确为完整票号字符串(末4位 = 绑定时填的票号后4位)。

## 🔎 开抢前 reserve/do 实测(2026-06-27,未到开抢)
- 对真实场次(reserve_id 35140 / 7-11 完整票号)发 reserve/do → `{"code":76651,"message":"当前预约通道拥挤，请稍后重试~"}`。
- 即**未开抢时也返回 76651**(无单独"未开始"码),归入火爆/重试类;**无 ptoken/gaia 验证码挑战**,form-urlencoded + csrf 即可。
- 结论:蹲点到 `reserve_begin_time` 后持续重试,开抢瞬间从 76651 翻成 0 即抢中。

## ⚠️ 仍需开抢时验证
- `reserve/do` 在真实开抢并发下是否触发额外风控(412/火爆码重试逻辑已就绪)。
- 场次 `state` / `online_state` 各值含义(当前样本均为 state:1, online_state:0,未开抢)。
- 商品场次(reserve_type=1)结构(预计与活动场次一致,待拉取确认)。
