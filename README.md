# Lineup Lab — public demo

A stateless, no-sign-in fantasy-football decision tool designed for Vercel.

## Public MVP

- Sleeper: username-based portfolio analysis
- ESPN: public league ID + team selection
- Yahoo: UI placeholder pending read-only OAuth credentials
- Exact league scoring, starting slots, repeated FLEX/SUPERFLEX, roster limits, waiver format, and draft context
- Lineup, waiver, trade, and injury recommendations
- No database and no saved accounts, credentials, rosters, or league data
- Private ESPN session cookies are deliberately unsupported
- Jev is deliberately omitted because the PromptQL integration requires authenticated identity

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt uvicorn
uvicorn api.index:app --reload
```

Open `http://127.0.0.1:8000` using `vercel dev`; the static UI is served from the repository root.

## Deploy

1. Import this GitHub repository into Vercel.
2. Framework preset: **Other**.
3. Root directory: repository root.
4. Deploy. No environment variables are required for Sleeper or public ESPN.

Yahoo OAuth should be added only after the first deployment establishes a stable callback URL.

## Privacy

Requests are processed in server memory. There is no application database. Upstream provider requests and normal hosting logs may still exist according to Sleeper, ESPN, and Vercel policies.
