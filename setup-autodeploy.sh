#!/usr/bin/env bash
# One-time setup for GitHub -> Cloud Run auto-deploy (run in Cloud Shell).
# Creates a deploy-only service account and lets ONLY the GitHub repo
# legaleagles/lawsticker-backend-gcp sign in as it, without any key file.
# Safe to run twice: steps that already exist are skipped.
set -u
PROJECT=gen-lang-client-0984437242
PROJECT_NUMBER=494996217030
REPO=legaleagles/lawsticker-backend-gcp
SA=github-deployer@$PROJECT.iam.gserviceaccount.com

gcloud config set project "$PROJECT" -q >/dev/null
echo "1/5 Turning on required Google services..."
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com run.googleapis.com -q

echo "2/5 Creating the deploy account..."
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || gcloud iam service-accounts create github-deployer --display-name="GitHub auto-deploy" -q

echo "3/5 Giving it deploy permissions..."
for r in run.admin iam.serviceAccountUser cloudbuild.builds.editor artifactregistry.writer storage.admin serviceusage.serviceUsageConsumer logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="roles/$r" --condition=None -q >/dev/null && echo "   ok  $r"
done

echo "4/5 Connecting GitHub (keyless)..."
gcloud iam workload-identity-pools describe github --location=global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create github --location=global --display-name="GitHub" -q
gcloud iam workload-identity-pools providers describe github-repo --location=global --workload-identity-pool=github >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc github-repo --location=global --workload-identity-pool=github \
    --display-name="lawsticker-backend-gcp" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='$REPO'" -q

echo "5/5 Allowing only $REPO to use it..."
gcloud iam service-accounts add-iam-policy-binding "$SA" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO" -q >/dev/null && echo "   ok"

echo ""
echo "=============================================="
echo " ALL DONE. Auto-deploy is ready."
echo " Every push to GitHub now goes live by itself."
echo "=============================================="
