# Deploy SRE AI Agent UI to Kubernetes

These manifests deploy the React UI and its server-side Express proxy in namespace `sre-ai`. The proxy keeps `SRE_API_KEY` server-side and forwards requests to the existing backend endpoint `/api/v1/investigate`.

## 1. Confirm backend Service name and port

```bash
kubectl get svc -n sre-ai
kubectl get deploy -n sre-ai
```

Edit `deployment.yaml` and set `BACKEND_BASE_URL` to the actual backend Service DNS and **Service port**, for example:

```yaml
- name: BACKEND_BASE_URL
  value: "http://<backend-service-name>.sre-ai.svc.cluster.local:<service-port>"
```

Do not append `/api/v1/investigate` to `BACKEND_BASE_URL`.

## 2. Build and push the UI image

Run from the project root (`sre-ai-agent-ui/`). Replace the registry/project/repository and tag with values used in your environment:

```bash
docker build -t xxxx/tfs-docker/sre-ai-agent-ui:1.0.0 .
docker push xxxx/tfs-docker/sre-ai-agent-ui:1.0.0
```

Then update `image:` in `deployment.yaml` to that exact image. If the cluster needs credentials to pull from Harbor, create/configure an imagePullSecret in namespace `sre-ai` according to your organization's process and add `imagePullSecrets` under `spec.template.spec`.

## 3. Create the API key Secret

Use your real backend API key. Do not commit the key into YAML or source control.

```bash
kubectl create secret generic sre-ai-ui-secret \
  -n sre-ai \
  --from-literal=SRE_API_KEY='YOUR_REAL_API_KEY' \
  --dry-run=client -o yaml | kubectl apply -f -
```

If the namespace does not exist, create it first only if that is appropriate for your cluster:

```bash
kubectl create namespace sre-ai
```

## 4. Apply and verify

```bash
kubectl apply -k k8s/
kubectl rollout status deployment/sre-ai-ui -n sre-ai
kubectl get pods,svc,pdb -n sre-ai -l app.kubernetes.io/name=sre-ai-ui
kubectl logs -n sre-ai deployment/sre-ai-ui --tail=100
```

Test privately with port-forwarding:

```bash
kubectl port-forward -n sre-ai svc/sre-ai-ui 8088:80
```

Open `http://localhost:8088` in your browser. Health check:

```bash
curl -i http://localhost:8088/healthz
```

Then run an investigation from the UI. The backend must be reachable from the UI pod using the configured Service DNS/port, and its API key must be correct.

## Notes

- The Service is `ClusterIP`; it is not internet-facing. Use your organization's authenticated Ingress/APISIX and TLS setup if external access is required.
- Authentication at that ingress is mandatory before exposing the UI externally: the proxy uses a shared backend API key, not per-user authentication. Do not add an unauthenticated route to the Service.
- The Deployment uses two replicas, non-root execution, a read-only root filesystem, dropped Linux capabilities, a RuntimeDefault seccomp profile, backend-aware readiness, liveness/startup probes, resource requests/limits, and a PodDisruptionBudget.
- The termination grace period is 130 seconds to cover the proxy's 120-second investigation timeout during graceful rollouts.
- Confirm your cluster's admission policies and image requirements before deploying. If a policy requires signed images, sign/attest the image through your approved pipeline.
- `BACKEND_BASE_URL` is a regular environment variable. `SRE_API_KEY` is read from a Kubernetes Secret and is never placed in browser code.
