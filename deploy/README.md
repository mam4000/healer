# Cloud Run deployment

This deployment runs the FastAPI/React web service and an on-demand task-worker
service on Cloud Run. Cloud Tasks dispatches no more than two enumerations at a
time and the worker scales to zero when idle. Memorystore Redis holds job status
and results, so polling and CSV downloads work across API instances.

## Provisioning

1. Copy `cloud-run.env.example` to `cloud-run.env`; `bcquery` is the default
   project. Fill in the shared-load-balancer backend audience, ChemQuery runtime
   service account, and Secret Manager secret name. Do not commit that file.
2. Authenticate `gcloud` with an account that can create Cloud Run, VPC,
   Memorystore, Artifact Registry, Secret Manager, and IAP resources.
3. Run `./deploy/deploy-cloud-run.sh`. The first run may stop after creating
   Memorystore because provisioning takes several minutes; run it again once
   the Redis instance reports `READY`.
4. Add a HEALER serverless NEG/backend service and `healer.<shared-domain>` host
   rule to the existing ChemQuery external HTTPS load balancer. Enable IAP on
   that backend service—not directly on Cloud Run—and give it the same IAP
   users/groups as ChemQuery. The backend's signed-header audience is the
   `IAP_JWT_AUDIENCE` value above.

The script sends the filtered source tree to **Cloud Build**. Cloud Build builds
the Linux image on Google hardware, publishes it to `bcquery`'s Artifact
Registry, and Cloud Run deploys that registry image; no local Docker image is
built or uploaded. It then provisions Cloud Tasks, the Redis endpoint in Secret
Manager, grants the shared-load-balancer IAP service account and ChemQuery
runtime service account Cloud Run invocation access, and deploys a
request-driven task service that scales to zero when no task is running.

## Operator runbook

No worker scaling is required. Cloud Tasks dispatches up to two concurrent jobs
to `healer-task-worker`, which scales to zero while idle. Redis retains results
for two hours. Monitor Cloud Run request/error logs, Cloud Tasks queue depth,
Redis memory, and set a billing-budget alert before sharing the URL.

## Verification

Open the shared `healer.<shared-domain>` hostname in an IAP-authorized Google
account. Submit two small test-set jobs, poll both until `SUCCESS`, download a
CSV, cancel a queued job, and confirm a third queued job remains `PENDING`.
