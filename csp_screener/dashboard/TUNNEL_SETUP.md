# Cloudflare Tunnel — access dashboard from your phone

Goal: expose your local dashboard (running at `http://localhost:8501`) to a
free HTTPS URL like `https://csp-screener-yourname.trycloudflare.com`. No
firewall changes, no port forwarding. Free forever.

## Why Cloudflare Tunnel (vs. ngrok or others)

- **Free, no signup needed** for quick tunnels
- **Permanent named tunnels** with a free Cloudflare account
- **HTTPS automatically** with Cloudflare's certs
- **No port forwarding** — works from any network, even behind NAT

## Quick start (no Cloudflare account, ephemeral URL)

1. **Install cloudflared** (one-time):
   ```cmd
   winget install --id Cloudflare.cloudflared
   ```
   Or download from https://github.com/cloudflare/cloudflared/releases

2. **Start the dashboard:**
   ```cmd
   cd "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent\csp_screener\dashboard"
   run_dashboard_background.bat
   ```

3. **Start the tunnel in a separate terminal:**
   ```cmd
   cloudflared tunnel --url http://localhost:8501
   ```

4. **Copy the URL** that prints (e.g. `https://random-words.trycloudflare.com`).
   Open it on your phone.

**Limitations of quick tunnels:**
- URL changes every time you restart cloudflared
- Public — anyone who knows the URL can see your dashboard
- Best for testing only

## Production setup (named tunnel + Cloudflare auth)

For a permanent URL like `https://dashboard.yourname.workers.dev` with
**Cloudflare Access** (login required), follow:
https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/

**Total cost: $0/month.**

Required only if you want:
- Permanent URL
- Auth gating (your dashboard is private to your email)
- Custom domain

## Run tunnel as Windows service (always-on)

```cmd
cloudflared service install --token YOUR_TOKEN
```

After installing the service, it auto-starts with Windows and reconnects on
network drops. Combined with the dashboard auto-start, you get a 24/7
phone-accessible dashboard for $0.

## Security warning

If you use a quick tunnel (no auth), DON'T leave it running 24/7. Random
people scanning trycloudflare.com URLs could find your dashboard. For
always-on, use the production setup with Cloudflare Access for auth.

## Mobile bookmark

Once you have a stable URL, add it to your phone's home screen for one-tap
access. Streamlit is mobile-friendly out of the box.
