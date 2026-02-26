# Model call interceptor

I use Dockerised LiteLLM running alongside Caddy in a Proxmox LXC to manage most of the Cloud LLM and my local vLLM server LLM endpoints for a number of reasons including not having to share so many secrets with my 'claw agents for example, and so I can rename models so that I don't to rename them in several services if I change my local vLLM models.

Caddy is for some ssl termination / port-num => hostname etc. and layer 7 filtering e.g. to my vLLM specific endpoints.  I use internal PKI and vlans and all that.
This LXC also hosts an API broker I use for some of the self-hosted services I use with my 'claws.
It's just the "interceptor" I'm pushing to this repo at the moment.

LiteLLM does support customised Python stages but I find it easier to have Claude Code make it's own interceptor for two main current uses:

1. Qwen3-30B-A3B-GPTQ-Int4 **/no_think** inserted into User prompts in requests if I match an alias with -NO-THINK appended.
  * I use the /no_think on my card for simplified hartbeats on specific agents for example or for low-latency chat etc. among other things.
2. Remove a "ghost" tooling issue from **MiniMax-M2.5** responses that was preventing me from using MiniMax to power a build-subagent in **OpenClaw**.

I'm using a Dockge (convenient for GitHub Copilot AND I) stack for caddy, litellm and this llm-interceptor.

Example compose file for this interceptor:

```yaml
services:
  llm-interceptor:
    build:
      context: /mnt/data/llm-interceptor/app
      dockerfile: Dockerfile
    image: llm-interceptor:local
    container_name: llm-interceptor
    restart: unless-stopped
    volumes:
      - /mnt/data/llm-interceptor/config.yaml:/config/config.yaml:ro
    environment:
      - LITELLM_URL=http://litellm:4000
      - CONFIG_PATH=/config/config.yaml
      - LOG_LEVEL=INFO
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health/readiness')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    # No ports exposed — only reachable from within caddy_default network

networks:
  default:
    name: caddy_default
    external: true
```

Another suggestion - if it's of any use to you, if you run OpenClaw as VM's on Proxmox locally, is that you don't have to run tailscale inside the VM.  If you have this proxying LXC anyway, why not have a tailscale exit here, and ssh tunnel from the LXC?  If you have different ISP's on different vlans, you can even just have Claude Code build in ISP failover, and LXC's are cheap - so have more on different machines for failover connections to the OpenClaw machine even if you haven't the resources for a cluster / proper high-availability.  I read on The Register some weeks ago, an article entitled "Vibe Coding. What is it good for?  Absolutely Nothing." (To paraphrase from memory).  Well I find it very helpful to quickly spin up solutions like this that are good enough for my homelab!  Having said that - this is rushed code and I haven't had much time for sleep for a while and I haven't reviewed it ... use at your own risk!


## ⚠️ AI-Generated Content Notice

This project was **modified with AI assistance** and should be treated accordingly:

- **Not production-ready**: Created for a specific homelab environment.
- **May contain bugs**: AI-generated code can have subtle issues.
- **Author's Python experience**: The author (modifier) is not an experienced Python programmer.

### AI Tools Used

- GitHub Copilot (Claude models)

### Licensing Note

Released under the **MIT License**. Given the AI-generated nature:
- The modifying author makes no claims about originality
- Use at your own risk
- If you discover any copyright concerns, please open an issue

---
