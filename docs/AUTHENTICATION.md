# Apple Ads API credentials

This server authenticates to the Apple Ads Platform API with the standard
self-service flow: an API user in your Apple Ads organization, an EC key pair
you generate, and the `clientId` / `teamId` / `keyId` Apple shows after you
upload the public key. No Apple approval or third-party app registration is
needed for a private deployment. Time: ~10 minutes.

There is **no read-only OAuth scope** in Apple Ads (the only scope is
`searchadsorg`); write capability is decided by the API user's *role*. This
server cannot write regardless (see PLAN.md §2), but give the API user a
read-only role anyway so Apple enforces it too.

## 1. Invite a read-only API user

An Account Admin does this in [Apple Ads](https://ads.apple.com):

1. Sign In → **Advanced**, choose the account (top-right Users menu).
2. **Account Settings → User Management → Invite Users**.
3. Enter the user's name and Apple Account (email). You can invite yourself
   under a second Apple Account, or a dedicated `ads-api@…` address.
4. Role: **API Account Read Only** (or *Limited Access API Read Only* if you
   want to restrict to specific campaign groups). Avoid *API Account Manager*
   and *Admin* for this deployment.
5. Send the invite; the user accepts it via the emailed secure code.

If the API user's role is later changed to a non-API role, API access is
revoked automatically.

## 2. Generate a key pair (on your machine)

```bash
openssl ecparam -genkey -name prime256v1 -noout -out private-key.pem
openssl ec -in private-key.pem -pubout -out public-key.pem
```

Keep `private-key.pem` secret. Never commit it, paste it into chat, or put it
in a Docker build argument.

## 3. Upload the public key

Signed in **as the API user**: **Account Settings → API**, paste the full
contents of `public-key.pem` (including the BEGIN/END lines), **Save**. Apple
then displays three values:

```
clientId SEARCHADS.xxxxxxxx-…
teamId   SEARCHADS.xxxxxxxx-…
keyId    xxxxxxxx-xxxx-…
```

These are `APPLE_ADS_CLIENT_ID`, `APPLE_ADS_TEAM_ID`, `APPLE_ADS_KEY_ID`.

## 4. Verify and find your ad account ID(s)

```bash
cp .env.example .env         # fill in the three IDs and APPLE_ADS_PRIVATE_KEY_PATH
set -a; source .env; set +a
python3 scripts/verify_credentials.py
```

The script requests a token, calls `GET /v1/me` and `GET /v1/acls`, and
prints the ad accounts this API user can see with their roles. Put the
numeric `id` value(s) you want this deployment to analyze in
`ALLOWED_ACCOUNT_IDS`. Accounts not listed there are refused even if the API
user can see them.

## How the server uses these

On each token request the server signs a short-lived JWT ("client secret")
with your private key — header `{alg: ES256, kid: keyId}`, claims
`iss=teamId`, `sub=clientId`, `aud=https://appleid.apple.com`, one-hour
expiry — and exchanges it at `https://appleid.apple.com/auth/oauth2/token`
(`grant_type=client_credentials`, `scope=searchadsorg`) for a bearer token
valid for one hour. Tokens are cached in memory only and refreshed under a
lock. Every ad-account-scoped call carries `X-AP-Context: adAccountId=<id>`,
derived only from an allowlisted account.

## Rotation

Generate a new key pair, upload the new public key (Account Settings → API →
Edit), update `APPLE_ADS_KEY_ID` and the private-key secret, redeploy, then
delete the old key material. If the private key may have leaked, do this
immediately; Apple's Terms make you responsible for the key.

## Troubleshooting

- `token request failed with HTTP 400/401`: `clientId`/`teamId`/`keyId` don't
  match the uploaded key, or the API user is inactive. Re-check the values in
  Account Settings → API for the *same* user whose key you uploaded.
- `could not sign client secret`: the PEM is not an EC P-256 private key or
  its newlines were mangled. Literal `\n` sequences are accepted; CRLF is not.
- `allowlisted account … is not visible to this API user`: the user was
  invited to a different account, or the invite was not accepted.
