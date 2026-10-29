# Implementation

Repository for implementation work.

## GitHub integration test

This README was created through the connected GitHub integration without cloning the repository.

Implementation details and setup instructions will be added as the project develops.

## GoldenGate deployment identity

A GoldenGate runtime has two names:

| Name | Source | Used for |
| --- | --- | --- |
| Runtime ID | Descriptor folder name, which is also the Helm release name | Kubernetes resources, Ingress host, `OGG_DOMAIN`, Argo CD Application, monitor registry key, and `OGG_DEPLOYMENT` as the bootstrap name |
| Registered deployment | The GoldenGate Service Manager on `/u02` | Service Manager links (`/services/<name>/<service>/...`) and the in-pod NGINX routes |

On a fresh `/u02`, GoldenGate registers the runtime ID, so the two names match. A
retained `/u02` keeps the name it was first registered under: the image creates
the deployment only when `/u02/Deployment/etc` is missing and never renames it.
No descriptor declares the registered name.

### Startup resolution

The chart mounts three files from `helm/goldengate/files/goldengate-identity/`
(ConfigMap `<runtime>-goldengate-identity`). Hooks use single-file `subPath`
mounts, so existing image hooks stay visible.

1. `${OGG_DEPLOYMENT_SCRIPTS}/setup/00-goldengate-identity.sh` is sourced by
   `/usr/local/bin/deployment-main.sh` before Service Manager and NGINX start.
   It records whether `/u02` is fresh. It also checks that the entrypoint renders
   NGINX through a bare `replace-variables.sh` call, and defines a
   `replace-variables.sh` shell function that takes precedence over the script.
2. `start_ogg` starts Service Manager and, on a fresh volume only, registers
   `OGG_DEPLOYMENT`.
3. When `start_nginx` calls `replace-variables.sh`, the function runs
   `/opt/goldengate-identity/resolve_deployment.py resolve`. The resolver:
   - queries `http://127.0.0.1:9011/services/v2/deployments` with the
     administrator credentials, read from the environment and never printed;
   - excludes `ServiceManager` and requires exactly one valid application
     deployment;
   - retries for up to 90 attempts, 2 s apart, while Service Manager starts.

   It exports `OGG_DEPLOYMENT=<registered name>` and calls the original
   `replace-variables.sh`.
4. `${OGG_DEPLOYMENT_SCRIPTS}/startup/99-goldengate-identity.sh` runs after NGINX
   starts. It checks that every rendered `/services/<name>/<service>` route uses
   the resolved name.

The container stops with a `goldengate-identity: ERROR:` log line, and NGINX
routes are never rendered with a guessed name, in any of these cases:

- no registered deployment, or more than one;
- an invalid name;
- a malformed response;
- rejected credentials;
- Service Manager unreachable after the retries;
- a fresh volume that registered an unexpected name;
- a setup hook that never ran.

Resolution only reads the Service Manager API. It never writes to `/u02` or the
registration.

### Pre-deployment check (read-only)

For each runtime, confirm the hook support and the single registration:

```bash
P="-n goldengate-dev <runtime>-0 -c <container>"
kubectl exec $P -- sh -c 'grep -n "run_user_scripts\|source\|^[[:space:]]*replace-variables.sh" /usr/local/bin/deployment-main.sh; ls -la /u01/ogg/scripts/setup /u01/ogg/scripts/startup'
kubectl exec $P -- sh -c 'curl -s --max-time 5 -u "$OGG_ADMIN:$OGG_ADMIN_PWD" http://127.0.0.1:9011/services/v2/deployments | python3 -c "import json,sys; print([i.get(\"name\") for i in json.load(sys.stdin)[\"response\"][\"items\"]])"'
```

The second command must print `ServiceManager` and exactly one other name.

### Deployment

The change modifies each runtime's pod template: it adds three ConfigMap
volumes and mounts, `OGG_DEPLOYMENT_SCRIPTS`, and a checksum annotation. For
the four DEV runtimes it also changes `OGG_DEPLOYMENT` back to the runtime ID.
Each runtime pod therefore restarts once when Argo CD syncs it. PVCs, EFS,
Services and Ingress are unchanged. Roll out one runtime at a time where
possible, and check after each:

- pod Ready, with `goldengate-identity:` log lines naming the registered deployment;
- `/api/processes` reports `registration.status: OK`;
- `tools/gg_api_contract_probe.py --check-routing` reports every registered
  route as `ROUTED`;
- the Service Manager UI links open the Administration, Distribution, Receiver
  and Performance Metrics services.

### Rollback

Revert the commit. Reverting only this change restores the descriptor-declared
`runtime.ogg.deploymentName` values from the previous release. Do not roll back
to a chart that has neither mechanism: its `OGG_DEPLOYMENT` would be the runtime
ID, and the Service Manager links of retained volumes would fail again
(OGG-12031). Neither direction touches `/u02` or the registrations.
