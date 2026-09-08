# Deploying to your own Google Cloud (Cloud Run)

This guide deploys a private, single-owner instance in **your** Google Cloud
project. Expected cost for personal use (a few sessions per week): **~$0/month**
(scale-to-zero, 1 max instance, well inside the free tier). Time: ~15 minutes
after you have your Apple Ads credentials (see `AUTHENTICATION.md`).

**Not a Google Cloud user?** The server is a plain container with an
env-var contract (see `.env.example`) — the same image runs on Fly.io,
Railway, Render, or any Docker host. This guide is simply the most
cost-guarded path we have tested end to end. Client connections are
documented separately in `CONNECT.md` and work identically wherever you host.

Never put secrets in Dockerfiles, git, build args, or shell commands that log
them. Secrets go into Secret Manager only.

## 0. Prerequisites

- [gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated:
  `gcloud auth login`
- Apple Ads credentials from `AUTHENTICATION.md`: `clientId`, `teamId`,
  `keyId`, your `private-key.pem`, and your numeric ad account ID.
- The API user should hold the **API Account Read Only** role.

## 1. Create a dedicated project

```bash
export PROJECT_ID=apple-ads-mcp-$RANDOM
gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID
# Link billing (needed even for free-tier usage):
gcloud billing accounts list
gcloud billing projects link $PROJECT_ID --billing-account=BILLING_ACCOUNT_ID

gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com
```

## 2. Store secrets in Secret Manager

```bash
printf '%s' 'SEARCHADS.YOUR_CLIENT_ID' | gcloud secrets create apple-ads-client-id --data-file=-
printf '%s' 'SEARCHADS.YOUR_TEAM_ID'   | gcloud secrets create apple-ads-team-id --data-file=-
printf '%s' 'YOUR_KEY_ID'              | gcloud secrets create apple-ads-key-id --data-file=-
gcloud secrets create apple-ads-private-key --data-file=private-key.pem

# MCP credential — pick ONE mode:
# bearer (recommended; Claude Code/API clients):
python3 -c "import secrets; print(secrets.token_urlsafe(32))" \
  | tr -d '\n' | gcloud secrets create mcp-access-token --data-file=-
# OR secret_path (claude.ai custom connectors):
# python3 -c "import secrets; print(secrets.token_urlsafe(32))" \
#   | tr -d '\n' | gcloud secrets create mcp-path-secret --data-file=-
```

Tip: `printf` (not `echo`) avoids trailing newlines corrupting the IDs. The
PEM file is uploaded as-is; the server accepts both real newlines and
literal `\n` sequences.

## 3. Dedicated least-privilege service account

```bash
gcloud iam service-accounts create apple-ads-mcp-runtime
export SA=apple-ads-mcp-runtime@$PROJECT_ID.iam.gserviceaccount.com

for s in apple-ads-client-id apple-ads-team-id apple-ads-key-id apple-ads-private-key mcp-access-token; do
  gcloud secrets add-iam-policy-binding $s \
    --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done
```

## 4. Build the container

```bash
gcloud artifacts repositories create mcp --repository-format=docker \
  --location=europe-west1
gcloud builds submit \
  --tag europe-west1-docker.pkg.dev/$PROJECT_ID/mcp/apple-ads-mcp:0.1.0
```

## 5. Deploy (personal-use cost profile)

```bash
gcloud run deploy apple-ads-mcp \
  --image europe-west1-docker.pkg.dev/$PROJECT_ID/mcp/apple-ads-mcp:0.1.0 \
  --region europe-west1 \
  --service-account $SA \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --concurrency 5 \
  --memory 512Mi --cpu 1 --timeout 120 \
  --set-env-vars "MCP_TRANSPORT=http,MCP_AUTH_MODE=bearer,\
ALLOWED_ACCOUNT_IDS=123456789" \
  --set-secrets "APPLE_ADS_CLIENT_ID=apple-ads-client-id:latest,\
APPLE_ADS_TEAM_ID=apple-ads-team-id:latest,\
APPLE_ADS_KEY_ID=apple-ads-key-id:latest,\
APPLE_ADS_PRIVATE_KEY=apple-ads-private-key:latest,\
MCP_ACCESS_TOKEN=mcp-access-token:latest"
```

Add `APPLE_ADS_REQUIRE_READONLY_ROLE=true` to the env vars if you want the
service to refuse to start when the API user holds a write-capable role
(default: it only logs a warning). The server cannot write either way.

`--allow-unauthenticated` refers to Google's IAM layer only — the application
itself rejects every request without your MCP credential. (An IAM-only
alternative exists for clients that can send Google identity tokens.)

For **secret_path** mode instead, replace the last two lines' auth pieces:
`MCP_AUTH_MODE=secret_path` and `MCP_PATH_SECRET=mcp-path-secret:latest`.

## 6. Connect your MCP client

```bash
export URL=$(gcloud run services describe apple-ads-mcp \
  --region europe-west1 --format='value(status.url)')
export TOKEN=$(gcloud secrets versions access latest --secret=mcp-access-token)
```

- **Claude Code** (bearer):

  ```bash
  claude mcp add --transport http apple-ads "$URL/mcp" \
    --header "Authorization: Bearer $TOKEN"
  ```

- **claude.ai custom connector** (secret_path mode): add a custom connector
  with URL `https://<service>/<MCP_PATH_SECRET>/mcp`. Treat that URL as a
  password.

Smoke test: `curl -s $URL/health` → `{"status":"ok"}`; `curl -s -o /dev/null
-w '%{http_code}' $URL/mcp` → `401` (bearer mode) proves auth is on.

## 7. Billing guardrails (do this)

```bash
gcloud billing budgets create --billing-account=BILLING_ACCOUNT_ID \
  --display-name="apple-ads-mcp" --budget-amount=5USD \
  --threshold-rule=percent=0.2 --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

Budget alerts are notifications, not hard caps. Emergency shutoff:

```bash
gcloud run services update apple-ads-mcp --region europe-west1 --max-instances 0
```

Monthly 2-minute check: Cloud Run billable time ≈ 0, Artifact Registry storage
(keep ≤3 images), active secret versions (≤6; destroy rotated-out versions),
no unexpected resources.

## 8. Rotation

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | tr -d '\n' \
  | gcloud secrets versions add mcp-access-token --data-file=-
gcloud run services update apple-ads-mcp --region europe-west1  # new revision
gcloud secrets versions destroy 1 --secret=mcp-access-token      # after verifying
```

Same pattern for the path secret (also update your connector URL). To rotate
the Apple private key, follow `AUTHENTICATION.md` → Rotation, then
`gcloud secrets versions add apple-ads-private-key --data-file=new-key.pem`
and `apple-ads-key-id` likewise.
