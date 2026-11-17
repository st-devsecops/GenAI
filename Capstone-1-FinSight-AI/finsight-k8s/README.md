# FinSight AI — Minikube Kubernetes manifests

These manifests deploy the existing FinSight API and Streamlit UI images:

- `standev001/genai:finsight-api-0.1.0`
- `standev001/genai:finsight-ui-0.1.0`

The API is intentionally one replica because ChromaDB and the semantic cache are
embedded/local storage. The API PVC mounts at `/app/storage`, matching the
provided API Dockerfile. The UI reaches the API through the in-cluster Service.

## 1. Prerequisites

Run from this directory with `kubectl` configured for your active Minikube
profile. Confirm the images are loaded:

```bash
minikube status
minikube image ls | grep 'standev001/genai'
```

If needed, load the local Docker images:

```bash
minikube image load standev001/genai:finsight-api-0.1.0
minikube image load standev001/genai:finsight-ui-0.1.0
```

## 2. Create the namespace, storage and ConfigMap

```bash
kubectl apply -f namespace.yaml
kubectl apply -f pvc.yaml
kubectl apply -f configmap.yaml
```

Wait for the PVC:

```bash
kubectl wait --for=jsonpath='{.status.phase}'=Bound pvc/finsight-api-storage -n finsight --timeout=120s
```

## 3. Create credentials as a Kubernetes Secret

Do NOT apply `secret.example.yaml` with its placeholder values. Create the Secret
from your own terminal. Use a long random API key and your Groq key if the
application is configured to use Groq.

```bash
read -s -p "FinSight API key: " FINSIGHT_API_KEY; echo
read -s -p "Groq API key (press Enter if not used): " FINSIGHT_GROQ_API_KEY; echo

kubectl create secret generic finsight-secrets -n finsight \
  --from-literal=FINSIGHT_API_KEY="$FINSIGHT_API_KEY" \
  --from-literal=FINSIGHT_GROQ_API_KEY="$FINSIGHT_GROQ_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

unset FINSIGHT_API_KEY FINSIGHT_GROQ_API_KEY
```

IMPORTANT: The exact backend authentication environment-variable name has not
yet been verified from `finsight/api/main.py`. The manifests pass the Secret
keys into the API container, but if backend authentication expects a different
name (for example, a plural key-list setting), update the Secret key and/or
API deployment env mapping to match the code before relying on authenticated
requests. The UI's `FINSIGHT_API_KEY` name is confirmed by `ui/streamlit_app.py`.

If you are using Ollama rather than Groq, this bundle does not configure access
from the Minikube container to Ollama running on the Mac host. That needs a
separate provider/network configuration based on `finsight/config.py`.

## 4. Deploy API and UI

```bash
kubectl apply -f api-deployment.yaml
kubectl apply -f api-service.yaml
kubectl apply -f ui-deployment.yaml
kubectl apply -f ui-service.yaml
```

Or apply all YAML manifests except the example Secret:

```bash
kubectl apply -f .
```

The latter is safe only after you have created `finsight-secrets`; it will also
apply the Secret template if you leave `secret.example.yaml` in this directory.
Prefer the individual commands above.

## 5. Verify

```bash
kubectl get all,pvc -n finsight
kubectl get pods -n finsight -o wide
kubectl describe pods -n finsight
kubectl logs deployment/finsight-api -n finsight
kubectl logs deployment/finsight-ui -n finsight
```

Wait for both deployments:

```bash
kubectl rollout status deployment/finsight-api -n finsight --timeout=180s
kubectl rollout status deployment/finsight-ui -n finsight --timeout=180s
```

Open the UI:

```bash
minikube service finsight-ui -n finsight
```

Alternatively:

```bash
minikube ip
```

Then open `http://<MINIKUBE-IP>:30085`.

## 6. Troubleshooting

- `ImagePullBackOff` / `ErrImageNeverPull`: verify the exact tags with
  `minikube image ls`; reload with `minikube image load`.
- `CrashLoopBackOff`: inspect `kubectl logs deployment/finsight-api -n finsight`
  and `kubectl logs deployment/finsight-ui -n finsight`.
- PVC pending: inspect `kubectl describe pvc finsight-api-storage -n finsight`
  and `kubectl get storageclass`.
- UI cannot reach API: check `kubectl get svc -n finsight` and API logs.
- Authentication failure: confirm the backend's expected key environment
  variable in `finsight/api/main.py`; the UI sends the key in `X-API-Key`.

## Notes

- This is a starter deployment bundle based on the Dockerfiles and variable
  names confirmed so far. It does not include Prometheus/Grafana installations.
- `secret.example.yaml` is documentation only. Do not commit real credentials.
- Keep API replicas at 1 while using embedded ChromaDB and SQLite cache.
