# FinSight AI - Kubernetes deployment bundle

This bundle contains deployment-related files only. It is intended to be copied into the root of the FinSight AI source repository, alongside `finsight/`, `ui/`, `scripts/`, `evaluation/`, `data/`, `requirements.txt`, and `requirements-dev.txt`.

## Included

- Hardened non-root API and Streamlit Dockerfiles.
- Prometheus scrape configuration and Grafana provisioning/dashboard.
- Kubernetes manifests for API, UI, Prometheus and Grafana.
- Kustomize base.
- GitHub Actions workflow: tests first, then builds and pushes immutable-SHA-tagged images to GHCR on pushes to `main`.
- `.dockerignore` to keep local environments and secrets out of image build contexts.

## Important integration notes

1. The uploaded archive contained only the `docker/` directory. The API/UI source code, requirements files, existing root `docker-compose.yml`, `.env`, and Git repository were not included. Therefore this is a deployment bundle, not a verified rebuild of the entire project. Docker builds and application-level tests must be run from the full repository root.
2. The API Dockerfile preserves the existing source layout assumptions: `finsight/`, `scripts/`, `evaluation/`, and `data/sample/` must exist at the repository root.
3. The API manifest uses `/health` for liveness/startup and `/ready` for readiness, consistent with the application endpoints previously identified. `/ready` can return 503 while auto-ingestion is in progress.
4. The API is deliberately one replica because the current Chroma/SQLite local storage layout is backed by one ReadWriteOnce PVC. Do not scale it horizontally without moving vector/cache state to shared external services and validating concurrency.
5. The Prometheus scrape assumes the API exposes `/metrics`. Confirm this endpoint in the complete application before relying on dashboard data.
6. The app's Ollama/Groq provider settings are supplied through a Kubernetes Secret created from the local `.env`. Ollama must be reachable from inside Minikube; `localhost` inside a pod refers to that pod, not the Mac host. Update the Ollama URL to a reachable endpoint before deployment if using local Ollama.
7. The API and UI use `FINSIGHT_API_KEY` from the Secret. The Grafana admin password is stored in a separate Secret.
8. The included workflow runs `pytest -q`. Add the project's evaluation-gate command after confirming the exact CLI and threshold flags in the full repository; the workflow does not guess those flags.

## 1. Copy into the full repository

Extract/copy the contents of this bundle into the project root, preserving paths. Review any existing files before replacing them.

## 2. Build locally (optional smoke check)

From the repository root:

```bash
docker build -f docker/Dockerfile.api -t finsight-api:local .
docker build -f docker/Dockerfile.ui -t finsight-ui:local .
```

## 3. GitHub / GHCR

1. Commit and push the deployment files to the repository's `main` branch.
2. Ensure GitHub Actions is enabled and the repository allows `GITHUB_TOKEN` to write packages.
3. The workflow runs tests, then publishes:
   - `ghcr.io/OWNER/REPOSITORY-api:sha-<commit>`
   - `ghcr.io/OWNER/REPOSITORY-ui:sha-<commit>`
4. Replace `OWNER/REPOSITORY` in `k8s/base/kustomization.yaml` with the lowercase GitHub owner/repository path. Set `newTag` to the exact `sha-<commit>` tag produced by Actions.

## 4. Create runtime secrets

Do not commit `.env` or Kubernetes Secret YAML files.

```bash
kubectl create namespace finsight
kubectl -n finsight create secret generic finsight-runtime --from-env-file=.env
kubectl -n finsight create secret generic finsight-grafana-secret --from-literal=admin-password='REPLACE_WITH_A_LONG_RANDOM_PASSWORD'
```

The `.env` must contain `FINSIGHT_API_KEY`. If a key is not configured for one of the optional providers, remove that optional key reference from `k8s/base/app.yaml` or create the Secret with the needed key. The manifest currently marks the Groq key reference optional and loads all other `.env` keys through `envFrom`.

## 5. Deploy to Minikube

Ensure Minikube is running and that your cluster can pull the GHCR packages. For private GHCR packages, configure an imagePullSecret and attach it to the `finsight` service account before deployment.

Edit `k8s/base/kustomization.yaml` to set both image names and the immutable tag, then:

```bash
kubectl apply -k k8s/base
kubectl -n finsight get pods -w
kubectl -n finsight get svc
```

Access the UI and Grafana:

```bash
minikube service finsight-ui -n finsight
minikube service finsight-grafana -n finsight
```

Prometheus is internal; use port-forward when needed:

```bash
kubectl -n finsight port-forward svc/finsight-prometheus 9090:9090
```

## 6. Verify

```bash
kubectl -n finsight rollout status deployment/finsight-api
kubectl -n finsight rollout status deployment/finsight-ui
kubectl -n finsight rollout status deployment/finsight-prometheus
kubectl -n finsight rollout status deployment/finsight-grafana
kubectl -n finsight get pvc
kubectl -n finsight logs deployment/finsight-api
```

Check API endpoints through a port-forward:

```bash
kubectl -n finsight port-forward svc/finsight-api 8000:8000
# In another terminal:
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/ready
curl -i http://127.0.0.1:8000/metrics
```

`/ready` may temporarily return HTTP 503 until the configured sample corpus finishes indexing. `/metrics` must return Prometheus exposition data for the scrape and dashboard to work.

## 7. Current limitations to resolve with the full repository

- Validate Docker build context paths and all Python dependencies against the actual repository.
- Confirm API metrics instrumentation and metric names; dashboard panels use standard Python process metrics and may need adjustment.
- Confirm the exact auto-ingest directory and ensure sample files are included in the image as expected.
- Configure an Ollama endpoint reachable from Minikube, or configure providers that do not depend on a host-local Ollama process.
- Configure image pull credentials if GHCR packages are private.
- The PVC currently provides a single 5Gi volume for `/app/storage` and `/app/data/uploads`; increase capacity based on corpus and upload sizes. For production, use separate claims and a managed storage class.
- This is a Minikube learning deployment. Production use needs TLS/Ingress, backup/restore, persistent Prometheus/Grafana storage, network policies, secret management, and a tested upgrade/rollback process.
