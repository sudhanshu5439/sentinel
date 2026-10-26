#!/usr/bin/env bash
# One-time toolchain setup for Sentinel inside WSL2 Ubuntu (24.04).
# Run from inside Ubuntu:  bash scripts/setup-wsl.sh
# Safe to re-run: each step skips tools that are already installed.
set -euo pipefail

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
ARCH=amd64

say "Base packages"
sudo apt-get update -y
sudo apt-get install -y build-essential make git curl wget unzip jq ca-certificates gnupg \
  lsb-release software-properties-common python3 python3-venv python3-pip

say "kubectl"
if ! have kubectl; then
  KVER=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
  curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl"
  sudo install -m 0755 /tmp/kubectl /usr/local/bin/kubectl
fi

say "kind"
if ! have kind; then
  curl -fsSLo /tmp/kind "https://github.com/kubernetes-sigs/kind/releases/latest/download/kind-linux-${ARCH}"
  sudo install -m 0755 /tmp/kind /usr/local/bin/kind
fi

say "Helm"
if ! have helm; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

say "Terraform (HashiCorp apt repo)"
if ! have terraform; then
  wget -qO- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor --yes -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
    | sudo tee /etc/apt/sources.list.d/hashicorp.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y terraform
fi

say "GitHub CLI (gh)"
if ! have gh; then
  sudo mkdir -p -m 755 /etc/apt/keyrings
  wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y gh
fi

say "Go (latest stable)"
if ! have go && [ ! -x /usr/local/go/bin/go ]; then
  GOVER=$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -n1)
  curl -fsSLo /tmp/go.tgz "https://go.dev/dl/${GOVER}.linux-${ARCH}.tar.gz"
  sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz
fi
grep -q '/usr/local/go/bin' ~/.bashrc || echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> ~/.bashrc

say "uv (fast Python package manager)"
if ! have uv; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

say "Claude Code (official native installer)"
if ! have claude; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
grep -q '.local/bin' ~/.bashrc || echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc

say "Docker check (Docker Desktop must be running with WSL integration enabled)"
if have docker && docker info >/dev/null 2>&1; then
  echo "Docker OK: $(docker version --format '{{.Server.Version}}')"
else
  echo "Docker NOT reachable. Open Docker Desktop > Settings > Resources > WSL integration > enable Ubuntu, then re-run."
fi

say "Done. Open a NEW Ubuntu terminal (or run: source ~/.bashrc), then check versions:"
cat <<'EOF'
  kubectl version --client && kind version && helm version --short && terraform version
  gh --version && go version && uv --version && python3 --version && claude --version && docker version
EOF
