# Sentinel: laptop setup (Windows 11, 16 GB RAM)

Total time: about 1.5–2 hours, mostly downloads. Do the steps in order.

## Step 1: Install WSL2 + Ubuntu (Windows side)

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart the laptop when asked. Ubuntu opens and asks you to create a Linux username and password.

## Step 2: Cap WSL memory so Windows stays usable

In Windows, create the file `C:\Users\<your-windows-user>\.wslconfig` (Notepad is fine) with:

```ini
[wsl2]
memory=10GB
processors=8
swap=8GB
```

Then in PowerShell run `wsl --shutdown` and reopen Ubuntu.

## Step 3: Install Docker Desktop

1. Download Docker Desktop for Windows from docker.com and install it.
2. Settings → General → tick **Use the WSL 2 based engine**.
3. Settings → Resources → **WSL integration** → enable **Ubuntu-24.04**.
4. Apply & restart. In Ubuntu, `docker run hello-world` should work.

## Step 4: Install VS Code

Install VS Code on Windows and add the **WSL** extension. From Ubuntu you can then open any
folder with `code .`

## Step 5: Run the setup script (Ubuntu side)

```bash
mkdir -p ~/projects && cd ~/projects
# copy the starter pack here first (see Step 7), then:
bash sentinel-starter/scripts/setup-wsl.sh
source ~/.bashrc
```

It installs kubectl, kind, Helm, Terraform, GitHub CLI, Go, uv and Claude Code, and checks Docker.

> **Important:** keep all project files under `~/projects` (the Linux filesystem). Working from
> `/mnt/c/...` is very slow with Docker and Kubernetes tools.

## Step 6: Git and GitHub

```bash
git config --global user.name "Sudhanshu Mishra"
git config --global user.email "<the email on your GitHub account>"
git config --global init.defaultBranch main
gh auth login          # choose GitHub.com → SSH → generate a new key → login with browser
```

Create both repos and clone the main one:

```bash
cd ~/projects
gh repo create sudhanshu5439/sentinel --public --clone --description "Self-healing, supply-chain-secured Kubernetes delivery platform with an LLM incident assistant"
gh repo create sudhanshu5439/sentinel-gitops --public --description "GitOps state for Sentinel (watched by Argo CD)"
```

## Step 7: Add the starter pack and make the first commit

Copy the starter pack contents into the repo. From Ubuntu, your Windows Downloads folder is at
`/mnt/c/Users/<your-windows-user>/Downloads/`.

```bash
cd ~/projects
unzip /mnt/c/Users/<your-windows-user>/Downloads/sentinel-starter.zip
cp -r sentinel-starter/. sentinel/
cd sentinel
git add -A
git commit -m "docs: add project plan, progress log and Claude Code instructions"
git push -u origin main
```

## Step 8: Start Claude Code

```bash
cd ~/projects/sentinel
claude
```

The first run opens a browser to log in. You need a Claude Pro or Max plan (or an Anthropic
Console/API account). The free plan doesn't include Claude Code.

Then paste the prompt from `prompts/phase-1-kickoff.md`.

## Daily routine with Claude Code

```bash
cd ~/projects/sentinel && claude
```

Start each session with: **"Read docs/PROGRESS.md and tell me where we are and what's next."**
End each session with: **"Update docs/PROGRESS.md and commit."**

Useful inside Claude Code:

- `/init`: Claude Code can scan the repo and suggest additions to CLAUDE.md later
- `/clear`: start a fresh context when switching to a new component
- Shift+Tab: cycle permission modes (keep the default "ask before edits" while learning)

## Stop the cluster when you're done for the day

```bash
make down        # once Phase 1 exists; until then: kind delete cluster --name sentinel
```

This frees the RAM for everything else on the laptop.
