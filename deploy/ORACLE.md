# Putting val:grind on an Oracle Always Free machine

Why here: it is the only host that is genuinely free forever *and* changes
nothing about how the app works. The public IPv4 is included, the disk is a
real disk so SQLite stays exactly as it is, outbound transfer is 10 TB a
month, and the architecture is supported — so not one line of code differs
from what runs on your laptop.

What it replaces: a Fly trial machine that was killed every five minutes,
which is why no scan ever finished. A full 100-match scan needs **about 13.5
minutes** uninterrupted (measured: 341s of ingest, 461s of resolve).

---

## Current status — steps 1 and 2 are DONE

The account exists and the machine is built and running. Confirmed over SSH
on 19 November 2026:

| | |
|---|---|
| Instance | `valgrind` — **Running** |
| Public IP | **129.159.174.254** |
| SSH user | `ubuntu` |
| Shape | VM.Standard.E2.1.Micro — Always Free-eligible |
| CPU / RAM | 1 OCPU (2 threads), 952 MB usable |
| OS | Ubuntu 24.04.5 LTS, x86_64 |
| Region / AD | us-ashburn-1, AD-2 |
| Network | 0.48 Gbps, public IPv4 included free |

```bash
ssh ubuntu@129.159.174.254
```

**Why the micro and not the 12 GB Arm machine.** Ampere A1 was out of host
capacity in *all three* Ashburn availability domains — AD-2, AD-1 and AD-3
were each refused by the API, despite the console claiming A1 was available
in any of them. This is the well-known Arm shortage. The micro is the
documented fallback and is genuinely fine here: the scan is network-bound,
and 952 MB is roughly what the Fly machine had while it ran Layer 5
successfully. A1 can be retried another day and the instance replaced.

One trap worth recording: the console filters the image list by the selected
shape, so once an Arm image is chosen the micro shape greys out as "not
compatible", and the Arm-only image list gives no way back. The fix is to
pick the **shape first**, while the image is still the x86 default, then
change the image.

**Do not click "Upgrade to Pay As You Go".** The account is currently limited
to Always Free resources, which is what makes accidental charges impossible.

### It is deployed and live

**<https://129-159-174-254.sslip.io>** — val:grind, behind the shared
password, on a real Let's Encrypt certificate.

Steps 3 to 8 below are done. What was actually built:

| | |
|---|---|
| Firewall | iptables 80/443 open and persisted, **and** VCN ingress rules added to the public subnet's security list |
| Docker | 29.1.3 with Compose 2.40.3, from Ubuntu's own packages |
| Stack | `app` + `caddy`, both running, app reachable only through Caddy |
| HTTPS | Let's Encrypt, verified from outside (`ssl_verify_result=0`) |
| Gate | 401 without the password, 200 with it |
| Survives reboot | **tested** — rebooted and back up unattended in 60s |
| Reclamation guard | systemd timer hourly, logging to `/var/log/clipsvalorant-keepalive.log` |

**No DuckDNS needed after all.** `129-159-174-254.sslip.io` is a free
wildcard-DNS hostname that resolves to this machine's IP, which is enough for
Let's Encrypt to issue a certificate. No signup, no token, no domain. If the
IP ever changes, the hostname changes with it — that is the one trade against
DuckDNS, and `.env` still has the DuckDNS fields ready if you'd rather switch.

**The keepalive is a systemd timer, not cron.** This image ships without the
`cron` package, and `crontab -` fails *silently* there — the worst possible
failure mode for the thing standing between you and reclamation. The timer
units are `clipsvalorant-keepalive.{service,timer}` in this directory.

---

## 1. Create the account

Go to <https://www.oracle.com/cloud/free/> and sign up.

**The one irreversible choice is your home region.** Your free resources only
exist there and it cannot be changed later. It matters more than it looks: a
scan makes about **9,000 outbound API calls**, so every extra 10ms of round
trip is potentially minutes of scan time. Pick the region closest to you and
your friends — `eu-frankfurt-1`, `ap-mumbai-1`, `ap-singapore-1`,
`us-ashburn-1` and so on.

Then generate an SSH key if you do not have one. On Windows:

```bash
ssh-keygen -t ed25519 -C "oracle-valgrind"
```

The public key you will paste is in `C:\Users\Omer\.ssh\id_ed25519.pub`.

---

## 2. Create the instance

Console → **Compute** → **Instances** → **Create instance**.

**Shape — try these in order.** Click *Change shape* → *Ampere* and ask for:

| Preference | Shape | OCPU / RAM | Notes |
|---|---|---|---|
| **First choice** | `VM.Standard.A1.Flex` | **2 OCPU / 12 GB** | Arm. 16× the CPU and 12× the RAM of the fallback, same $0. |
| Fallback | `VM.Standard.E2.1.Micro` | 1/8 OCPU / 1 GB | x86. Always available when Arm is not. |

Set the Arm shape to **exactly 2 OCPU and 12 GB** — that is what the Always
Free monthly allowance covers running 24/7 (1,500 OCPU-hours ÷ 730 ≈ 2.05,
and 9,000 GB-hours ÷ 730 ≈ 12.3). Asking for more silently makes it billable.

If you get **"Out of host capacity"**, that is normal for Arm and not
something you did wrong. Either retry over a few hours or take the E2.1.Micro
— the app is network-bound and barely touches the CPU, so 1/8 OCPU is
genuinely workable. *Everything below is identical for either shape*, because
the image is built on the machine and picks up its own architecture.

I verified the Arm question directly rather than guessing: every dependency
has an `aarch64` wheel on PyPI — `onnxruntime` 1.32.0, `numpy`, `pillow`,
`opencv-python`, `pydantic-core`, `shapely`, `pyclipper` — and
`rapidocr-onnxruntime` is pure Python. Nothing needs compiling from source.

**The rest of the form:**
- **Image:** Canonical Ubuntu 24.04 (the commands below assume it)
- **Boot volume:** 50 GB is fine. Always Free gives 200 GB total across all
  volumes, so don't take more than you need if you want a second instance.
- **Networking:** keep *Assign a public IPv4 address* — **yes**. This is the
  thing Google charges for and Oracle does not.
- **SSH keys:** paste your public key

Create it, then note the **public IP address** from the instance page.

```bash
ssh ubuntu@YOUR_PUBLIC_IP
```

---

## 3. Open the firewall — in both places

This is the step that wastes everyone's afternoon. Oracle filters traffic in
two independent layers and opening one does nothing on its own.

**Layer 1 — the virtual network.** Instance page → *Virtual cloud network* →
*Security Lists* → the default list → **Add Ingress Rules**, twice:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

**Layer 2 — the machine itself.** Oracle's Ubuntu images ship with iptables
rules that permit almost nothing but SSH:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
```

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
```

```bash
sudo netfilter-persistent save
```

(On an Oracle Linux image it is firewalld instead:
`sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload`.)

Docker publishes ports through its own chains and can sometimes reach a
container despite those INPUT rules, so this may appear unnecessary — do it
anyway rather than depend on that. You can confirm the whole path later with
the health check in step 8.

---

## 4. Get a hostname

Caddy needs a real hostname to get a certificate; Let's Encrypt will not
issue one for a bare IP. DuckDNS gives you one free, permanently.

Sign in at <https://www.duckdns.org/> with any of its login options, create a
subdomain (say `valgrind`), point it at your instance's public IP, and copy
your **token** from the top of the page.

---

## 5. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
```

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker ubuntu && newgrp docker
```

---

## 6. Get the code and write the secrets

```bash
sudo git clone https://github.com/omer-khawar/clipsvalorant.git /opt/clipsvalorant && sudo chown -R ubuntu:ubuntu /opt/clipsvalorant
```

```bash
cd /opt/clipsvalorant/deploy && cp .env.example .env && nano .env
```

Fill in `SITE_HOST` (the full `something.duckdns.org`), `DUCKDNS_DOMAIN` (just
the label) and `DUCKDNS_TOKEN`, your `HENRIK_API_KEY`, the two Twitch values,
and **set `CLIPS_PASSWORD`**. The machine is on a public hostname; without a
password anyone who finds it spends your API quota and can see whose matches
have been scanned.

---

## 7. Start it, and keep it started

```bash
sudo cp /opt/clipsvalorant/deploy/clipsvalorant.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now clipsvalorant
```

The first run builds the image, which pulls ffmpeg and a few hundred MB of
wheels. Expect several minutes, and longer on the 1/8-OCPU shape.

```bash
cd /opt/clipsvalorant/deploy && docker compose logs -f
```

Watch for `clipsvalorant UI listening on 0.0.0.0:8080` from the app and a
certificate line from Caddy. Then `Ctrl-C` — the containers keep running.

---

## 8. Guard against reclamation, and check the whole chain

Oracle may **reclaim an Always Free instance it considers idle**: over a
continuous 7-day window, 95th-percentile CPU under 20%, network under 20%,
and (Arm shapes only) memory under 20%. All of those must be true at once, so
defeating any one is enough — and a clip finder nobody uses for a week meets
all three.

`keepalive.sh` fetches the site hourly over its public hostname and refreshes
the DNS record. That is a genuine end-to-end health check which happens to
satisfy the policy — it exercises DNS, Caddy, the certificate and the app, so
a failed renewal shows up in a log instead of in a friend's complaint.

```bash
sudo cp /opt/clipsvalorant/deploy/keepalive.sh /usr/local/bin/clipsvalorant-keepalive && sudo chmod +x /usr/local/bin/clipsvalorant-keepalive
```

```bash
( crontab -l 2>/dev/null; echo "17 * * * * /usr/local/bin/clipsvalorant-keepalive >> /var/log/clipsvalorant-keepalive.log 2>&1" ) | crontab -
```

Run it once by hand to prove everything works end to end:

```bash
/usr/local/bin/clipsvalorant-keepalive
```

`healthz: 200` means DNS, both firewall layers, the certificate and the app
are all correct. Then open `https://your-subdomain.duckdns.org` and run a
scan — and let it finish, because for the first time nothing will interrupt
it at five minutes.

---

## Deploying a change later

```bash
cd /opt/clipsvalorant && git pull && sudo systemctl restart clipsvalorant
```

The unit rebuilds the image, so that is the whole deploy.

**This only works because `/opt/clipsvalorant` is a git checkout.** It was not
one for the first day of this machine's life — the code had been copied up by
hand, so `git pull` answered `not a git repository`, and every deploy was a
file written over SSH with nothing anywhere to say whether the box and the
repository still agreed. If you rebuild this machine, clone it; do not copy
it.

Expect **about 20 seconds** for a code change. The Dockerfile installs
dependencies from `pyproject.toml` before it copies any source, so a Python
edit reruns only the last two layers. Changing `pyproject.toml` reinstalls
onnxruntime and opencv and takes about four minutes on this shape — that is
the real cost, and it is now paid only when dependencies actually move.

Note that a restart also **drops a scan in progress** — the queue lives
in memory and nothing resumes. Deploy when nobody is mid-scan, or fix the
resumability first.

---

## The gotchas, in one place

| Trap | What happens | Avoid it by |
|---|---|---|
| "Upgrade to Pay As You Go" | Free allowances start billing | Never click it |
| Arm shape above 2 OCPU / 12 GB | Silently billable | Set exactly 2 and 12 |
| Only opening the VCN security list | Site unreachable, no obvious reason | Do the iptables step too |
| No public IPv4 assigned | No inbound, and no outbound without paid NAT | Keep the IP — it is free here |
| Home region picked carelessly | Permanent; adds latency across 9,000 calls | Choose near you at signup |
| Boot volumes over 200 GB total | Billable | 50 GB is plenty |
| Idle for 7 days | Instance reclaimed | Install the keepalive cron |
| `CLIPS_PASSWORD` left empty | Anyone spends your API quota | Set it before sharing the URL |
| Losing the `caddy_data` volume | A new certificate on every restart, then rate-limited | Don't `docker compose down -v` |

---

## What is still worth fixing in the code

Independent of hosting, and not done yet:

- **A scan keeps no progress.** Interrupt it at match 60 and all 60 are
  discarded. The `last_resolved_at` and `resolve_attempts` columns already
  track exactly what resuming needs — they are what produced the timings
  quoted at the top of this file.
- **`TYPICAL_SCAN_S`** is now 800 rather than 210, so the queue stops
  promising 3.5 minutes for ~13.5 minutes of work.
