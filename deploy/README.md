# Cloud Run deployment

This deployment runs the FastAPI/React web service and an on-demand task-worker
service on Cloud Run. Cloud Tasks dispatches no more than two enumerations at a
time and the worker scales to zero when idle. Memorystore Redis holds job status
and results, so polling and CSV downloads work across API instances.

## Provisioning

1. Copy `cloud-run.env.example` to `cloud-run.env`; `bcquery` is the default
   project. Fill in the comma-separated approved Google-account emails. Do not
   commit that file.
2. Authenticate `gcloud` with an account that can create Cloud Run, VPC,
   Memorystore, Artifact Registry, Secret Manager, and IAP resources.
3. Run `./deploy/deploy-cloud-run.sh`. The first run may stop after creating
   Memorystore because provisioning takes several minutes; run it again once
   the Redis instance reports `READY`.
4. If the project has no Google Cloud organization (as is currently the case for
   `bcquery`), or if an approved person is outside its organization, configure
   IAP's custom OAuth client once in the Cloud Console. In Cloud Run, open the
   service's **Security** tab, select **Edit policy**, then **Configure in IAP**;
   configure an **External** consent screen and choose **Auto generate
   credentials**. Until this is done, IAP shows “Empty Google Account OAuth
   client ID(s)/secret(s)” instead of a sign-in page.

   If the Cloud Console does not expose the IAP OAuth settings, download the
   JSON credentials for the newly created **Web application** OAuth client and
   run `./deploy/configure-iap-oauth.sh /path/to/client_secret.json`. The
   helper attaches the client only to `healer-web`, uses a mode-600 temporary
   settings file, and removes it when finished.

The script sends the filtered source tree to **Cloud Build**. Cloud Build builds
the Linux image on Google hardware, publishes it to `bcquery`'s Artifact
Registry, and Cloud Run deploys that registry image; no local Docker image is
built or uploaded. It then provisions Cloud Tasks, the Redis endpoint in Secret Manager, and direct IAP
on the default `run.app` URL, grants each listed user `roles/iap.httpsResourceAccessor`,
and deploys a request-driven task service that scales to zero when no task is
running.

## Operator runbook

No worker scaling is required. Cloud Tasks dispatches up to two concurrent jobs
to `healer-task-worker`, which scales to zero while idle. Redis retains results
for two hours. Monitor Cloud Run request/error logs, Cloud Tasks queue depth,
Redis memory, and set a billing-budget alert before sharing the URL.

## Verification

Open the emitted `run.app` URL in a listed Google
account. Submit two small test-set jobs, poll both until `SUCCESS`, download a
CSV, cancel a queued job, and confirm a third queued job remains `PENDING`.
