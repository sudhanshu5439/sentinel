# Sentinel: the front door. Run `make help` to see every target.
SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER   ?= sentinel
NAMESPACE ?= sentinel
SERVICES  := order-api risk-engine notifier
TAG       ?= dev
TF_LOCAL  := infra/terraform/envs/local-kind
SECRETS   := deploy/local/secrets.env

##@ Everyday

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

.PHONY: up
up: cluster images load deploy wait ## Create the cluster, build + load images, deploy everything
	@echo -e "\n\033[32mSentinel is up.\033[0m Try: make smoke"

.PHONY: down
down: ## Delete the local cluster (frees the RAM)
	terraform -chdir=$(TF_LOCAL) destroy -auto-approve

.PHONY: status
status: ## Show nodes, pods and services
	@kubectl get nodes -o wide
	@echo; kubectl -n $(NAMESPACE) get pods,svc -o wide

.PHONY: smoke
smoke: ## Place test orders end to end and check the notifier received them
	@bash scripts/smoke.sh

.PHONY: reload
reload: images load ## Rebuild images, load them and restart the apps (after code changes)
	kubectl -n $(NAMESPACE) rollout restart deploy/order-api deploy/risk-engine deploy/notifier
	$(MAKE) wait

.PHONY: logs
logs: ## Tail logs of one service: make logs SVC=order-api
	kubectl -n $(NAMESPACE) logs -f deploy/$(SVC) --tail=50

##@ Building blocks

.PHONY: cluster
cluster: ## Create the kind cluster with Terraform (1 control plane + 2 workers)
	@# Most changes to a kind cluster's definition force a full rebuild. Never let `make up` do that
	@# silently to a running cluster (it would wipe Argo CD and everything it deployed).
	@if kind get clusters 2>/dev/null | grep -qx $(CLUSTER); then \
	  echo "cluster '$(CLUSTER)' already exists: skipping Terraform (run 'make down' first to apply definition changes)"; \
	else \
	  terraform -chdir=$(TF_LOCAL) init -input=false -upgrade >/dev/null && \
	  terraform -chdir=$(TF_LOCAL) apply -input=false -auto-approve; \
	fi
	kind export kubeconfig --name $(CLUSTER)

.PHONY: images
images: ## Build the three service images
	@for s in $(SERVICES); do \
	  echo "==> building sentinel-$$s:$(TAG)"; \
	  docker build -q -t sentinel-$$s:$(TAG) apps/$$s || exit 1; \
	done

.PHONY: load
load: ## Load the images into the kind nodes (no registry needed locally)
	@for s in $(SERVICES); do kind load docker-image sentinel-$$s:$(TAG) --name $(CLUSTER); done

$(SECRETS):
	@umask 077; echo "POSTGRES_PASSWORD=$$(openssl rand -hex 16)" > $(SECRETS)
	@echo "generated $(SECRETS) (git-ignored)"

.PHONY: secrets
secrets: $(SECRETS) ## Generate local secrets once (never committed)

.PHONY: deploy
deploy: secrets ## Apply the Kubernetes manifests
	kubectl apply -k deploy/local

.PHONY: wait
wait: ## Wait until every workload is ready
	kubectl -n $(NAMESPACE) rollout status statefulset/postgres --timeout=180s
	kubectl -n $(NAMESPACE) rollout status deploy/redis --timeout=120s
	@for s in $(SERVICES); do kubectl -n $(NAMESPACE) rollout status deploy/$$s --timeout=180s || exit 1; done

##@ GitOps (Argo CD + Kyverno)

ARGOCD_VERSION ?= v3.5.6
GITOPS_RAW     ?= https://raw.githubusercontent.com/$(OWNER)/sentinel-gitops/main

.PHONY: gitops-up
gitops-up: gitops-secrets ## Install Argo CD and hand the cluster over to the sentinel-gitops repo
	kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -n argocd --server-side --force-conflicts \
	  -f https://raw.githubusercontent.com/argoproj/argo-cd/$(ARGOCD_VERSION)/manifests/install.yaml
	kubectl -n argocd patch configmap argocd-cm --type merge --patch-file deploy/argocd/application-health.yaml
	kubectl -n argocd patch configmap argocd-cmd-params-cm --type merge --patch-file deploy/argocd/server-insecure.yaml
	@# Not needed on a laptop: SSO (dex), ApplicationSets, notifications. Saves ~300 MB of RAM.
	kubectl -n argocd scale deploy argocd-dex-server argocd-applicationset-controller argocd-notifications-controller --replicas=0
	kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
	kubectl -n argocd rollout restart statefulset/argocd-application-controller
	kubectl apply -f $(GITOPS_RAW)/bootstrap/root.yaml
	@echo -e "\n\033[32mArgo CD is syncing from sentinel-gitops.\033[0m Watch it: make argocd-ui  /  make gitops-status"

.PHONY: gitops-secrets
gitops-secrets: ## Create the DB password Secret in staging and prod (never stored in Git)
	@for env in staging prod; do \
	  ns=sentinel-$$env; \
	  kubectl create namespace $$ns --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	  if ! kubectl -n $$ns get secret postgres-credentials >/dev/null 2>&1; then \
	    kubectl -n $$ns create secret generic postgres-credentials \
	      --from-literal=POSTGRES_PASSWORD=$$(openssl rand -hex 16) >/dev/null && echo "created secret in $$ns"; \
	  fi; \
	done

.PHONY: aiops-secrets
aiops-secrets: ## Create/refresh the assistant's Secret (Anthropic key, Slack webhook) from .env
	@test -f .env || { echo "missing .env (see aiops/README or docs/PROGRESS.md)"; exit 1; }
	@kubectl create namespace aiops --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@# Values are read from .env inside this shell and passed as files: they never appear in
	@# the process list, the terminal or Git.
	@set -a; . ./.env; set +a; tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	  printf '%s' "$$ANTHROPIC_API_KEY" > "$$tmp/ANTHROPIC_API_KEY"; \
	  printf '%s' "$$SLACK_WEBHOOK_URL" > "$$tmp/SLACK_WEBHOOK_URL"; \
	  kubectl -n aiops create secret generic sentinel-assistant \
	    --from-file="$$tmp/ANTHROPIC_API_KEY" --from-file="$$tmp/SLACK_WEBHOOK_URL" \
	    --dry-run=client -o yaml | kubectl apply -f - >/dev/null && echo "secret aiops/sentinel-assistant up to date"

.PHONY: aiops-status
aiops-status: ## Assistant health, recent pipeline log lines and the last incidents (in-cluster)
	@kubectl -n argocd get application aiops -o custom-columns=APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
	@kubectl -n aiops get pods -l app=sentinel-assistant
	@echo "--- recent log"
	@kubectl -n aiops logs deploy/sentinel-assistant --tail=200 | grep '"msg"' | jq -r '.ts + "  " + .msg' | tail -8
	@echo "--- incidents"
	@kubectl -n aiops port-forward svc/sentinel-assistant 18000:8000 >/dev/null 2>&1 & pf=$$!; sleep 2; \
	  curl -s localhost:18000/incidents | jq -r '.[:5][] | "\(.opened_at[:19])  \(.namespace)/\(.service)  \(.status)  \(.diagnosis.status // "pending") \(.diagnosis.diagnosis.category // "")  slack=\(if .notified_at then "sent" else (.notify_error // "-") end)"'; \
	  kill $$pf

.PHONY: aiops-off aiops-on
# Break-glass switch: a ConfigMap kept out of Git on purpose, so Argo CD self-heal cannot revert
# it. Mounted as a file the assistant re-reads before every diagnosis (kubelet syncs it in ~1 min).
aiops-off: ## Kill switch: stop all LLM calls without a deploy (evidence + Slack continue)
	kubectl -n aiops create configmap sentinel-assistant-switch --from-literal=LLM_ENABLED=false \
	  --dry-run=client -o yaml | kubectl apply -f -
aiops-on: ## Release the kill switch
	kubectl -n aiops delete configmap sentinel-assistant-switch --ignore-not-found

.PHONY: gitops-status
gitops-status: ## Show Argo CD apps, policies and the pods they run
	@kubectl -n argocd get applications -o custom-columns=APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision
	@echo; kubectl get imagevalidatingpolicies,validatingpolicies 2>/dev/null || true
	@echo; kubectl get pods -n sentinel-staging; echo; kubectl get pods -n sentinel-prod

.PHONY: argocd-ui
argocd-ui: ## Argo CD UI URL and admin password (served by Traefik, no port-forward)
	@echo "http://argocd.localtest.me:8080   user: admin   password: $$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"

.PHONY: policy-test
policy-test: ## Prove the policies: unsigned / tag-based / foreign images are refused in staging
	@bash scripts/policy-test.sh

##@ Canary releases and observability

ENV ?= staging

.PHONY: canary-bad
canary-bad: ## Release a version with errors through Git and watch the canary abort: make canary-bad ENV=staging RATE=0.3
	@bash scripts/canary.sh bad $(ENV) $(or $(RATE),0.3)

.PHONY: canary-slow
canary-slow: ## Release a slow version through Git: make canary-slow ENV=staging MS=500
	@bash scripts/canary.sh slow $(ENV) $(or $(MS),500)

.PHONY: canary-reset
canary-reset: ## Restore the healthy version through Git: make canary-reset ENV=staging
	@bash scripts/canary.sh reset $(ENV)

.PHONY: canary-watch
canary-watch: ## Follow the order-api rollout live: make canary-watch ENV=staging
	@bash scripts/canary.sh watch $(ENV)

.PHONY: rollouts-ui
rollouts-ui: ## Argo Rollouts dashboard URL (served by Traefik)
	@echo "http://rollouts.localtest.me:8080"

.PHONY: grafana-ui
grafana-ui: ## Grafana URL and admin password (served by Traefik)
	@echo "http://grafana.localtest.me:8080   user: admin   password: $$(kubectl -n monitoring get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d)"

.PHONY: alertmanager-ui
alertmanager-ui: ## Alertmanager URL: see which alerts are firing (served by Traefik)
	@echo "http://alertmanager.localtest.me:8080"

.PHONY: prometheus-ui
prometheus-ui: ## Prometheus URL: try http_requests_total (served by Traefik)
	@echo "http://prometheus.localtest.me:8080"

.PHONY: urls
urls: ## Every platform UI and whether it answers through the ingress
	@for h in grafana alertmanager prometheus argocd rollouts; do \
	  printf "%-14s http://%s.localtest.me:8080  -> HTTP %s\n" $$h $$h \
	    "$$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://$$h.localtest.me:8080/)"; \
	done

##@ Supply chain

OWNER ?= sudhanshu5439
.PHONY: verify
verify: ## Verify a published image's signature: make verify SVC=order-api
	cosign verify ghcr.io/$(OWNER)/sentinel-$(SVC):main \
	  --certificate-identity-regexp 'https://github.com/$(OWNER)/sentinel/.github/workflows/supply-chain.yml@.*' \
	  --certificate-oidc-issuer https://token.actions.githubusercontent.com | jq '.[0].optional | {Issuer, Subject}'
	gh attestation verify oci://ghcr.io/$(OWNER)/sentinel-$(SVC):main --owner $(OWNER)

##@ Quality

.PHONY: test
test: ## Run every service's unit tests
	cd apps/order-api && uv run pytest -q
	cd apps/notifier && uv run pytest -q
	cd apps/risk-engine && go test -race -count=1 ./...
	cd aiops && uv run pytest -q

.PHONY: lint
lint: ## Lint and format-check all code
	cd apps/order-api && uv run ruff check . && uv run ruff format --check .
	cd apps/notifier && uv run ruff check . && uv run ruff format --check .
	cd apps/risk-engine && test -z "$$(gofmt -l .)" && go vet ./...
	cd aiops && uv run ruff check . && uv run ruff format --check .
