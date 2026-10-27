"""Real Kubernetes clusters (any distribution: RKE2, K3s, EKS, AKS, GKE, OpenShift…) via the dynamic client."""
from __future__ import annotations

import asyncio
import tempfile
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, field_validator

from app.plugins import register_plugin

from . import model as m
from .base import KubernetesConfigBase, KubernetesConnector


class ClusterConfig(KubernetesConfigBase):
    auth_mode: Literal["kubeconfig", "in_cluster", "token"] = Field(
        "kubeconfig", description="kubeconfig: paste a kubeconfig; in_cluster: use the pod's service account; "
                                  "token: API server URL + bearer token")
    kubeconfig: SecretStr | None = Field(None, description="Full kubeconfig YAML (auth_mode=kubeconfig)",
                                         json_schema_extra={"ui:widget": "textarea"})
    context: str | None = Field(None, description="kubeconfig context; empty = current-context")
    server: str | None = Field(None, description="https://api.cluster:6443 (auth_mode=token)")
    token: SecretStr | None = Field(None, description="ServiceAccount bearer token (auth_mode=token)")
    ca_cert: str | None = Field(None, description="Cluster CA certificate PEM (auth_mode=token)",
                                json_schema_extra={"ui:widget": "textarea"})
    verify_tls: bool = True
    request_timeout_seconds: int = Field(30, ge=5, le=300)

    @field_validator("kubeconfig")
    @classmethod
    def _valid_kubeconfig(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None or not v.get_secret_value().strip():
            return v
        try:
            data = yaml.safe_load(v.get_secret_value())
        except yaml.YAMLError as exc:
            raise ValueError("kubeconfig is not valid YAML - paste it with its line breaks intact "
                             "(output of: kubectl config view --minify --raw)") from exc
        if not isinstance(data, dict) or not data.get("clusters"):
            raise ValueError("kubeconfig must contain a 'clusters' section")
        return v


_PATCH_CT = {"merge": "application/merge-patch+json", "json": "application/json-patch+json",
             "strategic": "application/strategic-merge-patch+json"}


@register_plugin
class KubernetesCluster(KubernetesConnector):
    type = "cluster"
    display_name = "Kubernetes cluster"
    description = "Any conformant Kubernetes cluster (RKE2, K3s, Rancher downstream, EKS, AKS, GKE, OpenShift)."
    Config = ClusterConfig

    def __init__(self, name: str, config: ClusterConfig):
        super().__init__(name, config)
        self._api = None
        self._dyn = None
        self._lock = asyncio.Lock()
        self._tmpfiles: list[Any] = []

    # -------------------------------------------------------------------------------- client setup
    def _build_client(self):
        from kubernetes import client, config as kconfig
        from kubernetes.dynamic import DynamicClient

        c: ClusterConfig = self.config
        cfg = client.Configuration()
        if c.auth_mode == "in_cluster":
            kconfig.load_incluster_config(client_configuration=cfg)
        elif c.auth_mode == "kubeconfig":
            if not c.kubeconfig:
                raise ValueError("kubeconfig is required for auth_mode=kubeconfig")
            kconfig.load_kube_config_from_dict(yaml.safe_load(c.kubeconfig.get_secret_value()),
                                               context=c.context or None, client_configuration=cfg,
                                               persist_config=False)
        else:
            if not (c.server and c.token):
                raise ValueError("server and token are required for auth_mode=token")
            cfg.host = c.server
            cfg.api_key = {"authorization": c.token.get_secret_value()}
            cfg.api_key_prefix = {"authorization": "Bearer"}
            if c.ca_cert:
                f = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)  # noqa: SIM115
                f.write(c.ca_cert)
                f.close()
                self._tmpfiles.append(f.name)
                cfg.ssl_ca_cert = f.name
        if not c.verify_tls:
            cfg.verify_ssl = False
        api = client.ApiClient(cfg)
        return api, DynamicClient(api)

    async def _clients(self):
        if self._dyn is None:
            async with self._lock:
                if self._dyn is None:
                    self._api, self._dyn = await asyncio.to_thread(self._build_client)
        return self._api, self._dyn

    async def _call(self, fn, *args, **kwargs):
        from kubernetes.client.exceptions import ApiException

        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ApiException as exc:
            if exc.status == 403:
                raise PermissionError(f"RBAC denied: {exc.reason}. Grant the copilot's service account access.") from exc
            raise RuntimeError(f"Kubernetes API error {exc.status}: {exc.reason}") from exc

    async def _resource(self, info: m.KindInfo):
        _, dyn = await self._clients()
        return await asyncio.to_thread(dyn.resources.get, api_version=info.api_version, kind=info.kind)

    @property
    def _timeout(self) -> int:
        return self.config.request_timeout_seconds

    # -------------------------------------------------------------------------------- primitives
    async def server_version(self) -> str:
        from kubernetes import client

        api, _ = await self._clients()
        info = await self._call(client.VersionApi(api).get_code, _request_timeout=self._timeout)
        return info.git_version

    async def _list_raw(self, info, namespace, label_selector):
        res = await self._resource(info)
        kwargs: dict[str, Any] = {"_request_timeout": self._timeout}
        if namespace and info.namespaced:
            kwargs["namespace"] = namespace
        if label_selector:
            kwargs["label_selector"] = label_selector
        result = await self._call(res.get, **kwargs)
        return [m.sanitize(info.kind, i) for i in (result.to_dict().get("items") or [])]

    async def _get_raw(self, info, name, namespace):
        from kubernetes.dynamic.exceptions import NotFoundError

        res = await self._resource(info)
        try:
            obj = await asyncio.to_thread(res.get, name=name, namespace=namespace, _request_timeout=self._timeout)
        except NotFoundError:
            return None
        return obj.to_dict()

    async def _pod_logs(self, name, namespace, container, tail_lines, previous, since_seconds):
        from kubernetes import client

        api, _ = await self._clients()
        kwargs: dict[str, Any] = {"tail_lines": tail_lines, "previous": previous, "timestamps": True,
                                  "_request_timeout": self._timeout}
        if container:
            kwargs["container"] = container
        if since_seconds:
            kwargs["since_seconds"] = since_seconds
        try:
            return await self._call(client.CoreV1Api(api).read_namespaced_pod_log, name, namespace, **kwargs) or "<no log output>"
        except RuntimeError as exc:
            if "400" in str(exc) and not container:
                return f"{exc}. The pod probably has several containers - pass the container name."
            raise

    async def _patch(self, info, name, namespace, body, dry_run, patch_type="merge"):
        res = await self._resource(info)
        kwargs: dict[str, Any] = {"body": body, "name": name, "content_type": _PATCH_CT[patch_type],
                                  "_request_timeout": self._timeout}
        if namespace:
            kwargs["namespace"] = namespace
        if dry_run:
            kwargs["dry_run"] = "All"
        obj = await self._call(res.patch, **kwargs)
        return {"dry_run": dry_run, "kind": info.kind, "name": name, "namespace": namespace,
                "resourceVersion": (obj.to_dict().get("metadata") or {}).get("resourceVersion")}

    async def _delete(self, info, name, namespace, dry_run):
        res = await self._resource(info)
        kwargs: dict[str, Any] = {"name": name, "namespace": namespace, "_request_timeout": self._timeout}
        if dry_run:
            kwargs["dry_run"] = "All"
        await self._call(res.delete, **kwargs)
        return {"dry_run": dry_run, "deleted": f"{info.kind}/{namespace}/{name}"}

    async def close(self) -> None:
        import os

        if self._api is not None:
            await asyncio.to_thread(self._api.close)
        for path in self._tmpfiles:
            try:
                os.unlink(path)
            except OSError:
                pass
