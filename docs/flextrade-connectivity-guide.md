# KelAI ↔ FlexTrade connectivity — practical guide

How we talk to the FlexTrade Brooklyn OMS (UAT and PROD) from KelAI code.
Grounded in the tracked samples under
[`vendor/flextrade/kelai_flex_sample_codes/`](../vendor/flextrade/kelai_flex_sample_codes/)
and the KOTL spec ([`working-order-book-spec.md`](working-order-book-spec.md), §7).

---

## 1. Big picture

- Transport is **gRPC** against FlexTrade's Brooklyn OMS. There is no REST layer.
- Two environments, **UAT** and **PROD**, selected by an `env` string at channel
  creation time. Same RPCs, different host endpoints.
- Auth is a **static bearer token passed as gRPC metadata** on every call (not
  TLS client certs — the channel itself is `insecure_channel`; connectivity is
  assumed to be over a private link / VPN). FlexTrade issues the token
  out-of-band (delivered by email); it is a JWT (`sub=JCO`, issuer
  "FlexTrade OMS", **no expiry claim**) and the canonical copy lives in AWS
  Secrets Manager under **`kelai/flextrade/api-token`** (us-east-1, account
  221082214032) together with the UAT/PROD endpoints:

  ```json
  {"token": "...", "uat_endpoint": "...", "prod_endpoint": "...",
   "metadata_key": "authorization", "scheme": "Bearer"}
  ```

  The gRPC metadata shape is `[("authorization", "Bearer " + token)]`.
- Everything (endpoints, token, protobuf stubs) comes from the FlexTrade
  **Brooklyn SDK**, which is **local-only and gitignored**:

| Piece | Where | In git? |
|---|---|---|
| Sample scripts (`kelai_sender.py`, `kelai_flex_utils.py`, `kelai_example_orders.py`) | `vendor/flextrade/kelai_flex_sample_codes/` | yes |
| Brooklyn SDK (`API` package: `Orders_pb2`, `Views_pb2`, `DomainCommons_pb2`, gRPC stubs, `.proto` sources) | `vendor/flextrade/brooklyn-12.57.0/` | **no (gitignored)** |
| Endpoints + auth (`API/Token.py`: `UAT`, `PROD`, `metadata`) | inside the SDK | **no — never commit** |
| API field browser | `vendor/flextrade/APIDocumentation.html` | no (gitignored) |

So on a fresh checkout the samples **do not run** until you drop the Brooklyn
SDK into `vendor/flextrade/` (or add its `flex_api/API` directory to
`sys.path`, which is what the samples' `sys.path.append` lines do).

## 2. Opening a channel

From `kelai_flex_utils.py`:

```python
import grpc
from API.Token import UAT, PROD, metadata   # endpoints + auth headers, local-only

grpc_options = [
    ("grpc.max_message_length", 512 * 1024 * 1024),
    ("grpc.max_receive_message_length", 512 * 1024 * 1024),  # big replay/query streams
    ("grpc.keepalive_time_ms", 330000),                      # keep long streams alive
]

def get_grpc_channel(env="PROD"):
    if env == "UAT":
        return grpc.insecure_channel(UAT, options=grpc_options)
    elif env == "PROD":
        return grpc.insecure_channel(PROD, options=grpc_options)
    raise ValueError("Invalid environment specified. Use 'UAT' or 'PROD'.")
```

Every RPC then passes `metadata=metadata` explicitly:

```python
stub = OrderService.OrderServiceStub(channel)
resp = stub.GetOrderInfo2(request, timeout=100, metadata=metadata)
```

Forgetting `metadata=` is the most common "it connects but returns
nothing/UNAUTHENTICATED" mistake.

## 3. Services and RPCs we use

| Need | Service.RPC | Helper | Notes |
|---|---|---|---|
| Submit orders | `OrderService.CreateOrders` | `kelai_sender.send_orders` | `sendToEms=True`, `PRE_TRADE` compliance rule set; response streams per-order results incl. `orderId` |
| Query orders by date | `OrderService.GetOrderInfo2` | `kelai_flex_utils.get_orders` / `get_orders_detailed` | streamed response; flattens parent → street → allocation |
| Positions snapshot | `OrderService.ReplayPositions` | `kelai_flex_utils.get_positions` | `sequenceId=0` replays all |
| Live P&L / blotter view | `ViewService.GenerateView` | `kelai_flex_utils.get_pnl` | `viewType=5`, `Portfolio` option (e.g. `KELAI`), streamed pages |
| Streaming order updates | `OrderService.Subscribe` | Brooklyn sample `subscribe_orders.py` | not used yet (KOTL v1 polls) |

All of these RPCs return **streams** — iterate the response, don't index it.

## 4. Sending an order (UAT walkthrough)

Order payloads are plain dicts (see `kelai_example_orders.py`):

```python
DASH_param_on_close = {
    "symbol": "DASH.US",          # Flex symbology: TICKER.COUNTRY
    "quantity": 123,              # unsigned; direction comes from side
    "side": "BUY",
    "orderType": "MARKET",
    "fund": "KELAI",
    "positionGroup": "USATop2000_strategy_v1",
    "user": "SFA", "owner": "SFA", "trader": "SFA",
    "manualFill": False,
    "brokerAutomationType": "AUTOROUTE",
    "timeInForce": "GFD",
    "algo": "VWAP_AMRS",
    "broker": "KEL-GS-EQ-LT",     # Goldman electronic low-touch route
}
```

`kelai_sender.send_orders(order_list=[...], _env="UAT")` builds one
`CreateOrdersRequest` for the whole batch:

```python
batchRequest = Orders_pb2.CreateOrdersRequest()
batchRequest.user = "MGO"
batchRequest.sendToEms = True                                   # actually routes
batchRequest.complianceInputs.ruleSets.append(DomainCommons_pb2.PRE_TRADE)
# ... one batchRequest.orders.add() per dict, field-by-field copy ...
response = stub.CreateOrders(batchRequest, timeout=40, metadata=metadata)
```

Practical points:

- **`sendToEms=True` means the order goes live** (to the EMS / street). For a
  "book it in Flex only" test, set it `False`.
- **Pre-trade compliance runs on Flex's side** (`PRE_TRADE` rule set); a
  rejected order comes back in the response, not as a gRPC error.
- **Capture `orderId` from the create response.** It is the only reliable join
  key for later fill queries — KOTL's whole refresh loop keys on it.
- Optional fields default in the sender: `tradingCurrency`/`settlementCurrency`
  → `USD`, `price` → `0`, empty strings for `trader`/`notes`/`broker`/`algo`/
  `fixTags`/`startTime`/`tradeDate`.

## 5. Reading back orders / fills

`get_orders(date, env)` and `get_orders_detailed(date, env)` wrap
`GetOrderInfo2`:

- **Date format matters**: the request wants `MM/DD/YYYY`; the helpers accept
  `YYYY-MM-DD` and convert. `fromDate == toDate` for a single day,
  `queryType = ALL_ORDERS`.
- The response is hierarchical: **parent order → street orders (per broker
  child) → allocations (per fund/prime)**. `get_orders` flattens one row per
  parent (last street order wins — lossy); `get_orders_detailed` emits one row
  per allocation (or per street order / per parent when children are absent).
- Key fill fields per parent: `orderId`, `quantity`, `filledQuantity`,
  `weightedAvgPrice`, `status`, `tradeDate`, `clientBatchIdentifier`.
- Enums (`side`, `status`, `orderType`) arrive as **Brooklyn enum ints** in raw
  exports — `ki_ops.kotl.enums` / `kelai_refresh.py` map them to labels, and
  `examples/kotl/get_order_info2_sample.json` mirrors the real UAT export shape.

`get_positions(env)` replays current positions into a DataFrame;
`get_pnl(user, portfolio_name, col_names, env)` pulls the live view (portfolio
`KELAI`) with whatever columns you name.

## 6. UAT smoke-test checklist

1. Brooklyn SDK present locally and importable (`import API.Orders_pb2` works).
2. `API/Token.py` has the UAT endpoint + current token — or better, pull both
   from Secrets Manager (`kelai/flextrade/api-token`) and build
   `metadata = [("authorization", "Bearer " + token)]` yourself. A bad/revoked
   token fails with `UNAUTHENTICATED`.
3. Network path to the UAT endpoint open (private link / VPN — plain TCP, no TLS).
4. Read-only probe first: `get_positions(env="UAT")` or
   `get_orders(today, env="UAT")` — proves channel + auth without side effects.
5. Send one small order via `send_orders(order_list=[...], _env="UAT")`; save
   the returned `orderId`.
6. Re-query with `get_orders(today, env="UAT")` and confirm the `orderId` shows
   up with expected `quantity` / `filledQuantity` / `status`.

Steps 5–6 are exactly KOTL's v1 acceptance loop (spec §12).

## 7. How this plugs into KOTL

`ki_ops.kotl.submit.FlexSubmitAdapter` is the seam:

```python
class FlexSubmitAdapter(Protocol):
    def create_orders(self, order_list: list[dict]) -> list[dict]: ...
```

- Offline (today): `FakeFlexAdapter` fabricates `orderId`s — no gRPC, no SDK.
- Live (next): a thin adapter wrapping `kelai_sender.send_orders` that returns
  `[{"success": ..., "orderId": ..., "symbol": ..., ...}, ...]` per order, plus
  a refresh source wrapping `get_orders(trade_date, env)` (the
  `kelai_refresh.py` flatten shape already matches).
- KOTL stamps `submit_id=<uuid>` into each order's `notes` field before send,
  so our-side grouping survives even without `clientBatchIdentifier`.

## 8. Gotchas and review notes

- **`batchRequest.user` is hardcoded to `"MGO"`** in `kelai_sender.py` while
  per-order `user`/`trader` come from the payload. A live adapter should make
  the batch user explicit config, not a literal.
- **`send_orders` drops `fund` and `owner`**: the example order carries both,
  but the sender never copies them onto the proto (only `positionGroup`).
  Verify against `Orders.proto` whether Flex derives fund from
  `positionGroup`, or whether these fields must be added.
- **Insecure channel + token in a plain file**: fine over a private link, but
  `Token.py` must stay out of git (it is, via the SDK gitignore) and off shared
  boxes. Treat the token like a password — the canonical copy is in AWS Secrets
  Manager (`kelai/flextrade/api-token`); a live adapter should read from there
  (or an env var populated from it) rather than importing `Token.py`. Note the
  JWT has **no `exp` claim**, so rotation only happens if FlexTrade revokes and
  reissues — ask them before assuming it's long-lived.
- **Responses are streams** — `send_orders` iterates the create response, but
  its trailing `hasattr(response, ...)` checks run on the *consumed stream
  object* and are effectively dead code. Don't copy that pattern; collect
  per-order results while iterating.
- **Timeouts**: 40 s on create, 100 s on queries, `None` (unbounded) on
  `GenerateView`. Long view streams rely on the 330 s keepalive.
- **`get_orders` (non-detailed) is lossy** when a parent has multiple street
  orders/allocations — the `_st` / `_acc_tgt` columns keep only the last child.
  Use `get_orders_detailed` when child-level truth matters.
- **Network reality check (2026-09-02, `kelai-ec2-preproc`)**: neither Flex
  endpoint is reachable from the preproc EC2 box. UAT (`172.20.194.76`) routes
  out the VPC default gateway and times out — no peering/VPN to FlexTrade's
  network exists there. PROD (`172.21.82.61`) is worse: a local **Docker bridge
  owns `172.21.0.0/16`**, so PROD traffic is blackholed on-box before it even
  leaves. Any host that will run the live adapter needs (a) the FlexTrade
  private link landed in its VPC/route table, and (b) Docker's
  `default-address-pools` moved off `172.20.0.0/14`. Working connectivity today
  is from the Mac where the Brooklyn SDK lives.
- **Platform quirk**: the Windows branch in `kelai_flex_utils.py` contains a
  placeholder path (`{your api package path/flex_api/API}`) and a stray
  indented line — the samples are effectively Linux/macOS-with-correct-
  `sys.path` only as written.

## 9. Network path — attempt 1: strongSwan on Linux (SUPERSEDED)

> **Superseded 2026-09-02** by the Windows FortiClient proxy (§9b). Blocked on
> the PSK, which is sealed in the encrypted sconf; the Windows path imports the
> sconf whole and never needs the PSK in the clear. The `flextrade-vpn` Linux
> instance (`i-03ff7e5d09b34d4f5`) is now unused — stop/terminate it (my
> role lacks `ec2:StopInstances`).

The Flex OMS endpoints are **private** (`172.20.194.76` UAT, `172.21.82.61`
PROD). Reaching them requires FlexTrade's **IPsec VPN**, not a public route.

**What the VPN actually is** (confirmed 2026-09-02 from the FortiClient tunnel
`IIPServices`): **IPsec IKEv1**, remote gateway **`35.245.224.15`** (public,
GCP), **pre-shared key + XAuth** (username/password). It is *not* SSL-VPN.

**FortiClient does not work for this on Linux.** The Linux FortiClient /
`forticlient-cli` is **SSL-VPN only** — the profile editor offers no VPN-type
choice and no PSK field (port defaults to 443). The IPsec tab exists only in
the macOS/Windows GUI. So on a Linux server the tunnel must be built with
**strongSwan** (IKEv1 + `xauth-psk`).

**Host decision:** run the tunnel on a **dedicated small instance**, not a
shared box. The preproc box already has a Docker bridge on `172.21.0.0/16`
(collides with Flex PROD) and other tenants; a system-wide IPsec tunnel there
is invasive. Recommended: a `t3.small` **Ubuntu 22.04/24.04** instance
(strongSwan is one `apt install` there; Amazon Linux 2023 has no strongSwan and
no EPEL by default). Give it an IAM role scoped to read
`kelai/flextrade/api-token`, and keep Docker off it (or pin
`default-address-pools` away from `172.20.0.0/14`).

**Credentials (as of 2026-09-02):** XAuth user `KelAI`, XAuth password (vendor
email), sconf-open password `SMEnorTo`. The **PSK is still outstanding** — it is
masked in the FortiClient GUI and sealed in the encrypted `.sconf`
(`s3://kelaitrading/flextrade/iip-vpn/`, `Enc `-prefixed AES-GCM; no supported
offline decryptor). Read it (and the Phase 1/2 proposals + Local ID + mode)
off the Mac's FortiClient advanced settings, or ask FlexTrade. **Secrets live
only in `/etc/swanctl/swanctl.conf` on the host (mode `600`) — never in git.**

**Provisioned host (2026-09-02, account 221082214032 / us-east-1):**

| Resource | Value |
|---|---|
| Instance | `i-03ff7e5d09b34d4f5` (`flextrade-vpn`, t3.small, Ubuntu 24.04) |
| Private / Public IP | `172.31.87.159` / `13.220.139.166` (default VPC, subnet `subnet-012a53a674e0a1359`) |
| Security group | `sg-029f15b4a75e3ea7f` (`flextrade-vpn-sg`: SSH from `172.31.0.0/16`, egress open) |
| IAM role / profile | `flextrade-vpn-role` / `flextrade-vpn-profile` (reads `kelai/flextrade/*` secrets only) |
| SSH key | `kelai-chroma-db` (SSH from the preproc box: `ssh -i ~/.ssh/kelai-chroma-db.pem ubuntu@172.31.87.159`) |
| Secrets | `kelai/flextrade/api-token`, `kelai/flextrade/vpn-credentials` (Secrets Manager) |

strongSwan + `xauth-generic` are installed; `/opt/flextrade/build-vpn-config.sh`
builds `/etc/swanctl/swanctl.conf` from the `vpn-credentials` secret and
initiates the tunnel. It currently **aborts because the `psk` field is empty** —
the only remaining step. To finish:

```bash
# 1. put the real PSK into the secret (keep other fields):
aws secretsmanager get-secret-value --secret-id kelai/flextrade/vpn-credentials --query SecretString --output text > /tmp/v.json
jq '.psk="<PSK>"' /tmp/v.json | aws secretsmanager put-secret-value --secret-id kelai/flextrade/vpn-credentials --secret-string file:///dev/stdin
# 2. on the host, build + initiate (override proposals/mode if Phase1/2 differ from defaults):
ssh ubuntu@172.31.87.159
sudo P1_PROPOSAL=aes256-sha256-modp1536 P2_PROPOSAL=aes256-sha256-modp1536 AGGRESSIVE=yes /opt/flextrade/build-vpn-config.sh
sudo swanctl --list-sas          # expect flextrade INSTALLED
nc -vz 172.20.194.76 50051       # UAT gRPC through the tunnel
```

**Setup outline** (template: [`vendor/flextrade/vpn/swanctl.conf.template`](../vendor/flextrade/vpn/swanctl.conf.template)):

```bash
sudo apt update && sudo apt install -y strongswan strongswan-swanctl libcharon-extra-plugins
# fill in swanctl.conf from the template (PSK, proposals, local id, remote subnets)
sudo install -m600 swanctl.conf /etc/swanctl/swanctl.conf
sudo systemctl enable --now strongswan
sudo swanctl --load-all
sudo swanctl --initiate --child flextrade
sudo swanctl --list-sas                       # expect INSTALLED, ESP up
```

Then verify from the host: `nc -vz 172.20.194.76 50051` (UAT gRPC). Traffic to
`172.20/172.21` should source from the mode-config virtual IP; if the gRPC
client can't reach Flex even with SAs up, add policy routing so those subnets
use the tunnel's source IP.

**Still open before the tunnel can come up:** (1) the PSK, (2) Phase 1
proposal + mode (FortiGate dialup is usually **aggressive**) + Local/Peer ID,
(3) Phase 2 proposal + PFS group, (4) the exact remote subnets (or whether
mode-config pushes them). All of these are in the Mac's FortiClient tunnel
settings or the sconf.

## 9b. Network path — current: Windows FortiClient + dumb TCP proxy

```
Linux (ki-ops / ECS)  ── TCP 172.31.85.215:50051 ──►  Windows Server 2022
                                                       netsh portproxy (dumb TCP)
                                                       FortiClient 7.0.1 IPsec (IIPServices)
                                                              │ IKEv1 agg + PSK + XAuth
                                                              ▼
                                                     35.245.224.15 ──► Flex UAT 172.20.194.76:50051
```

**Provisioned (2026-09-02, us-east-1):**

| Resource | Value |
|---|---|
| Windows instance | `i-058c546859266751c` (`flextrade-win-proxy`, t3.medium, Server 2022), private `172.31.85.215` |
| Proxy | `netsh portproxy` `0.0.0.0:50051 → 172.20.194.76:50051` (no parsing/buffering/retries; `iphlpsvc` auto-start) |
| SGs | `flextrade-win-proxy-sg` `sg-0c90af6421f62b431` (50051 from `flextrade-client-sg` `sg-09485e536f9ba3703` + preproc /32; RDP from VPC) |
| IAM | `flextrade-win-proxy-role` (SSM + read `kelai/flextrade/*` secrets + read sconf in S3) |
| Secrets | `kelai/flextrade/api-token`, `…/vpn-credentials`, `…/win-proxy-admin` (RDP Administrator) |
| Test harness | `~/flex_uat_test.py` on preproc (venv `~/flexvenv`, grpcio; token from Secrets Manager) |

**State:** sconf imported via `FCConfig -m all -o import -i 1 -p <sconf_password>`
(tunnel `IIPServices` in registry, PSK intact). `pktmon` capture of the IKE
exchange (2026-09-03) proves **IKEv1 Phase 1 aggressive-mode completes against
gateway `35.245.224.15` — PSK valid** (`I agg` → `R agg` → encrypted `I agg[E]`
on 4500), the client then enters the XAuth / mode-config round
(`phase 2/others R #6[E]` / `I #6[E]`) and immediately tears the SA down with an
informational delete (`I inf[E]`). No virtual adapter or `172.20.x` route ever
appears and `Test-NetConnection 172.20.194.76:50051` stays false.

The blocker is **headless XAuth under the SSM SYSTEM context**, not the password
itself (which is known: `Winter2026$`). Five injection methods were tried and
all reach the same Phase-1-OK / XAuth-teardown point: (1) raw plaintext into the
registry `Pass`, (2) sconf re-import to restore the Mac-encrypted `Pass`,
(3) partial-XML `importvpn`, (4) fresh XML import on this machine then
transplanting its encrypted `Pass`/`User` onto `IIPServices`, (5) feeding
user/pass to `ipsec.exe` on stdin. FortiClient's per-field password encryption
is bound to the interactive user's key store, which the headless `ipsec.exe`
service context can't reproduce or prompt for — so the saved credential never
decrypts at the XAuth step.

> **VERIFIED END-TO-END (2026-09-02):** after the one-time interactive connect,
> a real authenticated `OrderService.ReplayPositions` call from the preproc box
> returned **12 live UAT positions** for account `KELAI` (e.g. `NKE.US` −100, an
> AAPL put, `18880.KS`) — Linux → Windows `netsh` proxy → IPsec tunnel →
> Flex UAT `172.20.194.76:50051` → Brooklyn `OrderService`. Auth (bearer token
> from `kelai/flextrade/api-token`) and data path both proven. No orders sent.

**Zero-touch connect (2026-09-03, replaces the manual RDP step):** the box now
auto-logs-on `Administrator` at boot and the `FlexGuiAutomate` scheduled task
(at logon + every 10 min) drives the FortiClient GUI itself — it types
`xauth_username`/`xauth_password` fetched live from
`kelai/flextrade/vpn-credentials` into the login page and clicks Connect
(`gui_connect.ps1`, source of truth in the `kelai-infra` repo next to the CFN
stack, refreshed from `s3://kelaitrading/flextrade/win-automation/` at boot).
Verified: unattended reboot → tunnel up → `ReplayPositions` streamed live data
with no human involvement; a killed tunnel self-heals within one watchdog cycle.
Debug artifacts: screenshots in `s3://kelaitrading/flextrade/win-automation/`
and `C:\flex\gui_connect.log` on the box.

**Fail-closed (verified):** with the VPN down the proxy accepts TCP then the
connection dies — `172.20.194.76` is unroutable outside the tunnel, so traffic
can never fall back to the public internet.

**Real SDK runner:** `~/flex_real_test.py` on preproc. The Brooklyn python3 SDK
lives at `s3://kelaitrading/flextrade/1.1.brooklyn-12.57.0/` and is synced to
`/udata/home/jercoh/flex_sdk/python3` (both `python3/` and `python3/API/` on
`sys.path` — the generated stubs import siblings by bare name). Real gRPC path
is `/ft.OrderService/ReplayPositions` (proto package is `ft`, not `API`); server
reflection is disabled, so the path had to come from the stubs. The runner
points the channel at the proxy and pulls the token from Secrets Manager rather
than using the SDK's `API/Token.py` endpoints.

**Why GUI automation (correction of the earlier "headless reconnect" claim):**
a SYSTEM `ipsec.exe -b` re-dial with the GUI-saved credential only works while
a GUI session from the *current boot* is still warm. Verified 2026-09-02: after
a stop/start of the same instance (and equally on an AMI clone / new host), the
saved credential no longer decrypts — same `hr 80070002` XAuth teardown — so no
headless path survives a fresh boot. FortiClient only reliably accepts the
XAuth password through its GUI, which is why the automation types it there on
every boot instead of depending on FortiClient's saved credential. Caveat:
sconf has `disconnect_on_log_off=1`, so if you RDP into the console session,
close the window rather than logging off (a drop self-heals via the watchdog).

**Gaps:** FortiClient is 7.0.1.83 (Chocolatey's latest; 7.4.3 needs a manual
download from Fortinet's site if required — the sconf imported fine on 7.0.1).
The GUI automation clicks fixed coordinates (7.0.x login page, 1024x768), so a
FortiClient upgrade may need the offsets re-measured from the uploaded
screenshots. Recreation: golden AMI v2 `ami-03fa73d3274d4023a` via the CFN
stack in `kelai-infra` (`flextrade/windows-proxy/`) boots fully zero-touch.

## 10. Pointers

- KOTL spec + Flex mapping: [`working-order-book-spec.md`](working-order-book-spec.md) (§7–8)
- Order RPC definitions (local): `vendor/flextrade/brooklyn-12.57.0/python3/resources/Orders.proto`
- Field browser (local): `vendor/flextrade/APIDocumentation.html`
- Offline KOTL loop (no Flex needed): `ki-ops kotl submit-kelai … && ki-ops kotl refresh … && ki-ops kotl status …`
