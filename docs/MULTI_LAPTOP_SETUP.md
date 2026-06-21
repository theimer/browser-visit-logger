# Multi-laptop setup — from scratch

This is the end-to-end runbook for standing up the **multi-laptop**
configuration from nothing: create the EC2 VM, get the gRPC sync-server
running on it, and install the browser-visit logger on one or more laptops
so their databases converge.

Each per-project README documents its own piece in depth; this guide chains
them into one ordered sequence and fills the gap they leave open — minting
the TLS material that ties the VM and the laptops together. Where detail
lives elsewhere, this guide links to it rather than repeating it:

- [`browser-visit-sync-server/README.md`](../browser-visit-sync-server/README.md) — the gRPC service
- [`browser-visit-logger/README.md`](../browser-visit-logger/README.md) — the laptop extension + native host
- [`browser-visit-tools/README.md`](../browser-visit-tools/README.md) — `manage_vm.py`, `manage_sync_server.py`, `enroll_machine.py`, `install_laptop.sh`

> **Single-laptop mode needs none of this.** If you just want local logging
> on one Mac, run `browser-visit-logger/install.sh` and stop — see that
> project's README. Multi-laptop mode is opt-in and only activates once
> `~/.browser-visit-logger/config.json` exists on a laptop.

---

## What you end up with

```
  laptop-a ─┐
            ├─ gRPC/mTLS :50443 ─►  EC2 VM  (sync-server, systemd)
  laptop-b ─┘                       canonical DB + per-machine log mirror
```

One EC2 Linux VM runs the sync-server. Every laptop pushes its log records
to it and pulls every peer's records back, so each laptop's local
`~/browser-visits.db` converges to the union of all laptops' activity within
~1 minute of any interaction. Authentication is mTLS: one CA signs the
server cert and one client cert per laptop; the server pins each client
cert's SHA-256 to a `machine_id`.

## Prerequisites

**On your admin machine** (where you run the `manage_*` tools — typically
one of the laptops):

- **AWS credentials** from the ambient chain (env / `~/.aws` / SSO) with the
  permissions listed under "Prerequisites" in
  [`browser-visit-tools/README.md`](../browser-visit-tools/README.md#prerequisites)
  — EC2 run/describe + security groups, IAM role/instance-profile create
  **including `iam:PassRole`**, SSM `SendCommand` + `StartSession`, S3 on the
  `bvl-sync-deploy-*` bucket, and STS.
- **`boto3`** — `pip install boto3`.
- **Go 1.22+ and `protoc`** — to cross-compile the server binary
  (`make build-linux`). Alternatively build it in Docker via
  `browser-visit-sync-server/deploy/Dockerfile`.
- **`openssl`** — to mint TLS material.
- **The AWS Session Manager plugin** — for the one manual VM-shell step in
  [Step 4](#step-4--enroll-each-laptop). Install:
  <https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html>

**On each laptop**: macOS with the Xcode command-line tools (Swift
toolchain), Chrome/Chromium, and Python 3 (the system `python3` is fine).

All `manage_*` commands below are run from the `browser-visit-tools/`
directory.

### AWS credentials & permissions

The tools have no `--profile` flag — they read the **ambient** boto3 chain
(env vars / `~/.aws` / SSO). Two gotchas trip up a fresh setup:

- **Select a profile.** If you authenticate with AWS SSO (IAM Identity
  Center), `aws sso login` signs in a *named* profile but doesn't make it the
  default, so boto3 finds nothing and you get
  `NoCredentialsError: Unable to locate credentials`. Log in and export the
  profile in the **same shell** you run the tools from:

  ```bash
  aws sso login --profile <your-sso-profile>
  export AWS_PROFILE=<your-sso-profile>
  aws sts get-caller-identity          # confirm the chain resolves
  ```

  `export` lasts only for that terminal (add it to `~/.zshrc` to persist), and
  SSO sessions expire — just re-run `aws sso login` when credentials vanish.

- **`PowerUserAccess` is not enough.** That common SSO role grants everything
  *except* IAM writes, but `manage_vm.py create` must create an IAM role +
  instance profile and call `iam:PassRole` (so the VM is reachable over SSM).
  Under PowerUserAccess the create stops at the IAM step with `AccessDenied`.
  Use a role that also allows `iam:CreateRole`, `iam:CreateInstanceProfile`,
  `iam:AddRoleToInstanceProfile`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`,
  and **`iam:PassRole`** (admin is simplest) — or have someone with IAM rights
  pre-create the `bvl-sync-server-role` + `bvl-sync-server-profile` once, after
  which PowerUserAccess can launch against the existing profile.

If `aws sso login` or `manage_vm.py create` errors out, the
[Troubleshooting](#troubleshooting) section at the end walks through every
AWS sign-in / credential / IAM / launch error seen in practice, with the fix.

---

## Step 1 — Create the VM

Pick the CIDR(s) allowed to reach the gRPC port (your laptops' egress IPs).
`create` is idempotent — it find-or-creates a tagged instance, the security
group (gRPC `50443` only, **no SSH/22**), and an SSM-capable IAM instance
profile.

```bash
cd browser-visit-tools

# arm64/Graviton is the default (cheaper; the pure-Go server has no x86 deps),
# so the flags below are optional. For Intel use --arch x86_64 (→ t3.small).
python3 manage_vm.py create --allow-cidr <your-cidr>/32 \
    --region us-west-2 --arch arm64 --instance-type t4g.small

python3 manage_vm.py status
```

`status` prints the instance's `public:` DNS name and IP. **Record that
address** — it's both the SAN you bake into the server cert (Step 2) and the
endpoint each laptop connects to (Step 5). State is cached in
`~/.browser-visit-logger/vm.json`; every later command reads it (and
falls back to tag-discovery if it's stale).

> The `--arch` you choose here must match the `GOARCH` you build in Step 3:
> `x86_64 → amd64`, `arm64 → arm64`.

### Choosing `--allow-cidr`

`--allow-cidr` (required, repeatable) is the **network allowlist**: each value
becomes one security-group rule allowing inbound TCP to gRPC port `50443` from
that IP range. It's the only thing the VM exposes — there is no SSH. Think of
it as defense in depth, *not* the primary auth: mTLS is the real boundary
(an unenrolled or spoofed cert is rejected even from an allowed IP), so the
CIDR just stops the rest of the internet from opening a connection at all.

A CIDR is `<address>/<prefix>` — the prefix is how many leading bits are
fixed: `/32` = exactly one IPv4 address, `/24` = 256, `/16` = 65,536,
`0.0.0.0/0` = the whole internet.

The VM sees the **public egress IP** of whatever network the laptop is on
(everything behind one home/office NAT shares a single public IP), not the
laptop's private `192.168.x.x`. Find a network's public IPv4 with:

```bash
curl https://checkip.amazonaws.com
```

Reasonable choices:

- **A `/32` per network you use** (recommended) — tight, and `create` is
  additive, so just re-run it from each new location:

  ```bash
  python3 manage_vm.py create --allow-cidr 203.0.113.7/32      # home
  python3 manage_vm.py create --allow-cidr 198.51.100.40/32    # office (appends)
  # or several at once: --allow-cidr A/32 --allow-cidr B/32
  ```

- **A documented office egress block** — use it directly, e.g.
  `--allow-cidr 198.51.100.0/24`.

- **`0.0.0.0/0`** — defensible *here* because mTLS is the actual auth, but you
  lose the network filter (the world can attempt handshakes, showing up as
  `[caller=?]` rejections in the logs). Reach for it only if per-`/32`
  upkeep becomes a real nuisance while roaming.

Two gotchas:

- **This tool only *adds* rules** (duplicates are ignored); it can't remove
  them. To drop a stale CIDR (an old café IP, a changed home IP), use the AWS
  console or `aws ec2 revoke-security-group-ingress` on the
  `bvl-sync-server-sg` group.
- **IPv4 only** — the rule uses `CidrIp`. If a laptop reaches the VM over
  IPv6, it won't match any rule; if one network mysteriously can't connect,
  compare `curl -4 checkip.amazonaws.com` with `curl -6 …`.

## Step 2 — Mint TLS material

One CA signs everything. The same `gen-certs.sh` the integration tests use
takes a `--server-host` flag for production, so the server cert's SAN
matches the real VM address.

You supply exactly two things: the VM's stable endpoint and one
machine-id per laptop.

```bash
cd browser-visit-sync-server/test

# --server-host: the VM's DNS name (or public IP) — goes into the server
#   cert SAN so laptops can verify the server. Use a STABLE address (an
#   Elastic IP or DNS name): a raw EC2 public address changes on stop/start
#   and would no longer match the cert. Repeatable (pass a name AND an IP).
# Positional args: one machine-id per laptop (see the constraint below).
./gen-certs.sh --server-host <vm-dns-or-ip> laptop-a laptop-b
```

**The machine-id must equal that laptop's sanitised `LocalHostName`.** It
becomes the cert's `CN`, and the server rejects any request whose claimed
`machine_id` doesn't match the cert CN. Get a laptop's value by running this
*on that laptop* before you mint its cert:

```bash
scutil --get LocalHostName | sed 's/[^A-Za-z0-9_-]/-/g; s/--*/-/g; s/^-//; s/-$//'
```

(Or run `install_laptop.sh` on it first — it prints `Machine ID: <id>` — then
mint a cert with that exact CN.) The machine-id is the *only* per-laptop input
you provide; the private keys are generated for you.

This writes `./certs/`:

| File | Goes to | Role |
|---|---|---|
| `ca.crt` | VM (as `clients-ca.crt`) **and** every laptop (as `server-ca.crt`) | Trust anchor for both directions |
| `ca.key` | **kept offline and safe** | Signs all certs; the root of trust — **you need it to add laptops later** |
| `server.crt` / `server.key` | VM | Server identity (SAN includes your `--server-host`) |
| `<id>.crt` / `<id>.key` | the matching laptop | That laptop's client identity (`CN=<id>`) |
| `<id>.sha256`, `fingerprints.tsv` | (reference) | DER SHA-256 used at enrollment |

> **⚠️ A normal run mints a fresh CA. Run it once, then keep `ca.crt` +
> `ca.key`.** `gen-certs.sh <machine-ids…>` begins with `rm -rf certs/` and
> generates a **new CA every time** — re-running it to "add a laptop" would
> invalidate the server cert and every existing laptop, forcing a full
> re-provision. As a safety net, if a CA already exists the script **asks for
> confirmation first** (and refuses outright when run non-interactively)
> unless you pass `--force`. Still: mint your production material **once** and
> stash `ca.crt`/`ca.key` somewhere safe. To **add a laptop later**, use the
> non-destructive `--add-client` mode (next), not a fresh run.

### Adding a laptop later (incremental, no downtime)

When you buy a new machine, sign one more client cert against the **existing**
CA — nothing already deployed changes, so there's no server redeploy and the
other laptops are untouched. From the directory holding your saved
`ca.crt` + `ca.key` (i.e. the original `certs/`):

```bash
cd browser-visit-sync-server/test          # where your saved certs/ lives
./gen-certs.sh --add-client <new-machine-id>
```

`--add-client` reuses `certs/ca.{crt,key}`, leaves the CA + server cert alone,
writes just `certs/<new-machine-id>.{crt,key,sha256}`, and appends to
`fingerprints.tsv`. It refuses to run if there's no existing CA, or if combined
with `--server-host` / positional ids. Then **enroll** the new cert (Step 4)
and **install** on the new laptop (Step 5) — both are additive. The running
server trusts the new cert the moment it's enrolled.

## Step 3 — Build, provision, and start the server

Cross-compile the binary for the VM's architecture, then provision (first-
boot setup: `bvlsync` user, data dirs, systemd unit, TLS material) and
deploy in one shot.

```bash
# Match the VM arch from Step 1 (arm64 for arm64, amd64 for x86_64).
make -C browser-visit-sync-server build-linux GOARCH=arm64

cd browser-visit-tools
python3 manage_sync_server.py provision \
    --server-cert ../browser-visit-sync-server/test/certs/server.crt \
    --server-key  ../browser-visit-sync-server/test/certs/server.key \
    --clients-ca  ../browser-visit-sync-server/test/certs/ca.crt \
    --and-deploy  --binary ../browser-visit-sync-server/bin/sync-server-linux-arm64

python3 manage_sync_server.py start
python3 manage_sync_server.py health
```

`health` should report the service active, port `50443` open, and disk under
90%. Everything runs over AWS SSM — no SSH. Note the CA file is passed as
`--clients-ca` (laptops are the clients) and lands on the VM as
`/etc/browser-visit-sync/clients-ca.crt`. See
[`browser-visit-sync-server/README.md`](../browser-visit-sync-server/README.md#run-on-ec2-mtls-enrolled-machines-only)
for the on-disk layout and the systemd unit.

## Step 4 — Enroll each laptop

The server only accepts a client cert whose SHA-256 is recorded in the VM's
`/var/lib/browser-visit-sync/enrolled_machines.db`. There is no
SSM subcommand for this yet, so enrollment is the one manual step: build the
DB locally with `enroll_machine.py`, then place it on the VM using the
infrastructure that's already there (the S3 staging bucket the deploy tool
uses, plus an SSM Session Manager shell).

```bash
cd browser-visit-tools
pip install cryptography     # enroll_machine.py needs it to read PEM certs

# One row per laptop. machine-id must match the cert CN from Step 2.
for id in laptop-a laptop-b; do
    python3 enroll_machine.py --db ./enrolled_machines.db \
        --machine-id "$id" \
        --cert ../browser-visit-sync-server/test/certs/$id.crt
done
python3 enroll_machine.py --db ./enrolled_machines.db --list   # sanity-check

# Transfer to the VM via the per-account staging bucket the deploy uses.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2                                  # the VM's region
BUCKET="bvl-sync-deploy-${ACCOUNT}-${REGION}"
aws s3 cp ./enrolled_machines.db "s3://${BUCKET}/enrolled_machines.db" --sse

# Open a shell on the VM (no SSH — this is SSM Session Manager) and install it.
INSTANCE=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.browser-visit-logger/vm.json')))['instance_id'])")
aws ssm start-session --target "$INSTANCE"
#   --- on the VM ---
#   sudo aws s3 cp s3://<bucket>/enrolled_machines.db /var/lib/browser-visit-sync/enrolled_machines.db
#   sudo chown bvlsync:bvlsync /var/lib/browser-visit-sync/enrolled_machines.db
#   sudo systemctl restart sync-server
#   exit
```

To add a laptop later, re-run `enroll_machine.py` (it's `INSERT OR REPLACE`,
so existing rows are preserved) and repeat the transfer. To revoke one, use
`enroll_machine.py --revoke` and re-transfer. See the `enroll_machine.py`
section in
[`browser-visit-tools/README.md`](../browser-visit-tools/README.md#enroll_machinepy).

## Step 5 — Install on each laptop

On every laptop, run the multi-laptop installer pointed at the VM. It runs
the upstream `install.sh` (builds/signs the native host, installs the Chrome
manifest) and writes `~/.browser-visit-logger/config.json` with this
laptop's `machine_id` and the sync endpoint.

```bash
bash browser-visit-tools/install_laptop.sh --server <vm-dns-or-ip>:50443
```

The installer prints the resolved `machine_id` (sanitised
`scutil --get LocalHostName`). **It must match the cert CN and the enrolled
`machine_id` from Steps 2 and 4** — if not, mint/enroll under the printed
name (or override with `BVL_MACHINE_ID`).

Then drop that laptop's TLS material into `~/.browser-visit-logger/tls/`,
renaming to the names the sync client expects:

```bash
install -m 0700 -d ~/.browser-visit-logger/tls
cp <id>.crt  ~/.browser-visit-logger/tls/client.crt
cp <id>.key  ~/.browser-visit-logger/tls/client.key
cp ca.crt    ~/.browser-visit-logger/tls/server-ca.crt
chmod 600 ~/.browser-visit-logger/tls/client.key
```

Finally do the per-laptop Chrome steps from
[`browser-visit-logger/README.md`](../browser-visit-logger/README.md):
load the unpacked extension at `chrome://extensions`, and grant the macOS
Files & Folders (TCC) prompts the first time you tag a page.

> **Converting a laptop that already ran single-laptop mode?** Its historical
> snapshots live in the iCloud archive (`~/Documents/browser-visit-logger/snapshots/`),
> whereas multi-laptop mode uses the shared Google Drive archive — so those
> older snapshots won't be visible until you migrate them. Run the one-time,
> idempotent migration on that laptop (dry-run first):
>
> ```bash
> python3 browser-visit-tools/migrate_icloud_to_gdrive.py --dry-run \
>     --src ~/Documents/browser-visit-logger/snapshots \
>     --dst "~/Library/CloudStorage/GoogleDrive/My Drive/browser-visit-logger/snapshots" \
>     --db  ~/browser-visits.db
> ```
>
> It copies the archive to Google Drive (SHA-256 verified), rewrites the DB
> paths, and leaves the iCloud copy in place. Fresh laptops with no prior
> single-laptop history can skip this. See
> [`browser-visit-tools/README.md`](../browser-visit-tools/README.md#migrate_icloud_to_gdrivepy).

## Step 6 — Verify end to end

1. On `laptop-a`, browse to a page and tag it (★ / ✓ / ~) from the popup.
2. Within ~60s the sync fires. Confirm the server received it:

   ```bash
   cd browser-visit-tools
   python3 manage_sync_server.py logs --lines 50
   ```

   Look for a line like
   `[caller=laptop-a] …/PushLogs status=OK … lines=N`. A `[caller=?]` line
   means the cert wasn't enrolled; `[caller=…] status=PermissionDenied`
   means it's enrolled but the `machine_id` didn't match the cert CN. See
   the log-format notes in
   [`browser-visit-sync-server/README.md`](../browser-visit-sync-server/README.md).

3. On `laptop-b`, trigger any interaction (so its sync runs) and confirm the
   `laptop-a` row arrived — query via the `browser-visits` MCP `query` tool,
   or directly:

   ```bash
   sqlite3 ~/browser-visits.db 'SELECT url FROM visits ORDER BY rowid DESC LIMIT 5;'
   ```

You can also diff a laptop against a fresh VM snapshot with
`fetch_vm_snapshot.py` + `db_diff.py` — see
[`browser-visit-tools/README.md`](../browser-visit-tools/README.md#db_diffpy).

---

## Day-2 operations

| Task | Command |
|---|---|
| Health probe | `python3 manage_sync_server.py health` |
| Service status | `python3 manage_sync_server.py status` |
| Recent logs | `python3 manage_sync_server.py logs --lines 100 [--since '1 hour ago']` |
| Live log tail | `aws ssm start-session --target <instance-id>` then `journalctl -u sync-server -f` |
| Restart after a config change | `python3 manage_sync_server.py restart` |
| Ship a new binary | `make -C browser-visit-sync-server build-linux GOARCH=<arch>` then `manage_sync_server.py deploy --binary …` |
| Stop / start the VM (save cost) | `python3 manage_vm.py stop` / `start` |
| Tear it all down | `python3 manage_vm.py terminate --purge-infra` |
| Add a laptop later | `gen-certs.sh --add-client <id>` (incremental — see [Step 2](#adding-a-laptop-later-incremental-no-downtime)), enroll it (Step 4), `install_laptop.sh` on it (Step 5) |

**Stopping the VM** keeps the data volume; its public address may change on
restart — re-run `manage_vm.py status` and, if it changed, update each
laptop's `~/.browser-visit-logger/config.json`. To avoid this, attach a
stable DNS name / Elastic IP and use that as the `--server-host` in Step 2.

---

## Troubleshooting

Everything below was hit (and fixed) standing up a real VM. They cluster into
three phases: **(A)** getting AWS credentials to resolve, **(B)** getting the
IAM permissions to create infra, and **(C)** the `RunInstances` call itself.

### A. AWS sign-in & credentials

**`NoCredentialsError: Unable to locate credentials`** — boto3 found nothing
in the chain. With SSO this almost always means either you haven't run
`aws sso login`, or you have but didn't select the profile. `aws sso login`
signs in a *named* profile without making it the default, so you must
`export AWS_PROFILE=<profile>` in the same shell (see the
[credentials box](#aws-credentials--permissions) above). `aws sts
get-caller-identity` must print your account/role before any tool will work.

**`aws sso login` fails at `RegisterClient` / `StartDeviceAuthorization`** —
several distinct causes, easiest to rule out first:

- *`InvalidRequestException … Couldn't find Identity Center Instance`* — the
  decisive one: your `sso_start_url` in `~/.aws/config` doesn't resolve to a
  real Identity Center instance, or `sso_region` points at the wrong region.
  A valid start URL is the **AWS access portal URL** (`https://d-xxxxxxxxxx.awsapps.com/start`
  or `https://<subdomain>.awsapps.com/start`), *not* one built from the
  instance id (`…/ssoins-…`). Get the real URL and the instance's home region
  from the IAM Identity Center console → **Settings**, then re-run
  `aws configure sso` (reuse session name `my-dev-sso`) to rewrite the profile.
  A stale cached *token* can mask a wrong start URL until the session expires —
  so this can surface suddenly on a previously-working setup.
- *`AccessDeniedException` on `RegisterClient` / `InvalidRequestException` with
  an empty message* — usually an **old AWS CLI**. v2.22+ defaults to the
  authorization-code-with-PKCE flow and validates the start URL as an OIDC
  issuer; older versions register clients in a way newer Identity Center
  rejects. Upgrade the CLI (`brew install awscli`, then point your `aws` at it)
  and clear the SSO client cache so it re-registers fresh:
  `mv ~/.aws/sso/cache ~/.aws/sso/cache.bak` and log in again. As a stopgap on
  a correct-but-old setup, `aws sso login --use-device-code` forces the legacy
  device-code flow.

**SSO sign-in demands a passkey/MFA you don't have** — if the browser prompts
for a passkey that isn't on this device, try your phone (passkeys sync via
iCloud Keychain / your password manager) or "use another method." As the
account admin you can also reset it: IAM Identity Center → Users → your user →
**Multi-factor authentication**, delete the stale device, register a new one.

### B. IAM permissions for `create`

**`AccessDenied … iam:GetRole` (or `CreateRole`/`PassRole`) on `bvl-sync-server-role`**
— your role can't touch IAM. `PowerUserAccess` excludes IAM by design, and
`manage_vm.py create` (and `terminate --purge-infra`) must create the VM's
role + instance profile and `PassRole` it to EC2. Fix per the
[credentials box](#aws-credentials--permissions): assign yourself an
`AdministratorAccess` permission set for the account and use that profile for
the privileged steps, or add a scoped inline policy granting the IAM actions
listed there. **After changing a permission set, re-provision it and re-run
`aws sso login`** — a cached session keeps the old permissions.

> Only `create` and `terminate --purge-infra` need IAM. The routine commands
> (`start`/`stop`/`status`, all of `manage_sync_server.py`) run fine under
> `PowerUserAccess`, so you can switch back to it for day-to-day use.

### C. `RunInstances` errors

**`InvalidParameterValue … Invalid IAM Instance Profile name (bvl-sync-server-profile)`**
— not a misconfiguration: a freshly-created instance profile takes a few
seconds to become visible to EC2, and `create` launches immediately after
creating it. Just **re-run `create`** (it's idempotent — it reuses the now-
propagated profile) or wait ~15s. Confirm the profile is real with
`aws iam get-instance-profile --instance-profile-name bvl-sync-server-profile`.

**`IdempotentParameterMismatch` on `RunInstances`** — fixed in the current
tool (the EC2 client token is now unique per invocation). If you see it on an
**older checkout**, it's because the token was keyed only on region and EC2
caches a token→parameters binding for ~24h: re-creating after a terminate with
a *different* arch/type collides. Either pull the fix, change `--region`, or
wait out the 24h cache.

### Architecture

The tool **defaults to arm64/Graviton** (`t4g.small`) — cheaper, and the
pure-Go server has no x86 dependency. A no-flag `create` lands on arm64; build
with `GOARCH=arm64` to match. For Intel pass `--arch x86_64` (→ `t3.small`)
and build `GOARCH=amd64`. `manage_sync_server.py deploy` refuses a binary
whose arch token doesn't match the VM's recorded arch, so a mismatch fails
loudly rather than booting a broken service.
