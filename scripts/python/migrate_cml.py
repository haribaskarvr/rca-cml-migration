#!/usr/bin/env python3
"""Export and import Revenue Cloud CML using sf CLI authentication.

Catalog and context dependencies must already exist in the target org.
Imports are dry runs unless --apply is supplied. No activation or deletion.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


# Shared model defaults and the state passed from preflight to apply.
CONSTRAINT_MODEL_DEFAULTS = {
    "UsageType": "Constraint",
    "ResourceInitializationType": "Off",
    "InterfaceSourceType": "Constraint",
}


class MigrationError(RuntimeError):
    pass


@dataclass
class ImportPlan:
    api_name: str
    context: dict
    model: dict | None
    definition: dict | None
    version: dict | None
    existing: list[dict]
    links: list[dict]
    old_content: bytes | None
    content: bytes
    missing: list[dict]
    desired: list[dict]
    directory: Path

    @property
    def content_changed(self) -> bool:
        return self.old_content != self.content


# Keep authenticated requests on the org origin and report progress immediately.
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def progress(message: str, phase: str = "RUN", *, error: bool = False) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{phase}] {message}", file=sys.stderr if error else sys.stdout, flush=True)


# CLI options and top-level execution, in the order a run proceeds.
def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = command.add_subparsers(dest="command", required=True)
    for name in ("export", "import", "migrate"):
        operation = subcommands.add_parser(name, help=f"{name.capitalize()} CML model and associations")
        operation.add_argument("--auth-mode", choices=("sf-cli", "jwt"), default="sf-cli",
                               help="Reuse authenticated sf aliases (default), or log in using role-specific JWT environment variables")
        operation.add_argument("--api-version", help="REST API version; defaults to latest available per org")
        if name in ("export", "migrate"):
            operation.add_argument("--source-org", required=True, help="Source sf alias; created/refreshed in JWT mode")
            operation.add_argument("--model-api", required=True, help="Source ExpressionSet API name")
            operation.add_argument("--source-version", help="ExpressionSetDefinitionVersion Salesforce ID (15 or 18 characters) or version number; required if multiple exist")
            operation.add_argument("--product-key", default="External_Id__c", help="Product2 stable key field")
            operation.add_argument("--output-dir", required=True, help="Parent directory for automatic timestamped run folders")
        if name in ("import", "migrate"):
            operation.add_argument("--target-org", required=True, help="Target sf alias; created/refreshed in JWT mode")
            if name == "import":
                operation.add_argument("--input-dir", required=True, help="Exported run folder containing model.cml and manifest.json")
                operation.add_argument("--output-dir", help="Parent for the new run folder; defaults to the input folder's parent")
            operation.add_argument("--target-model-api", help="Override target model API name")
            operation.add_argument("--context-definition", help="Override target context developer name")
            operation.add_argument("--mapping-file", help="JSON: object name -> source record ID -> target record ID")
            operation.add_argument("--update-existing", action="store_true", help="Allow updating an explicitly selected inactive target version")
            operation.add_argument("--target-version", help="Target definition-version ID or number for updates")
            mode = operation.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true", help="Validate and report only (default)")
            mode.add_argument("--apply", action="store_true", help="Write to the target after preflight; never activates")
            operation.add_argument("--allow-production", action="store_true", help="Additional opt-in required for production writes")
    return command


def main() -> int:
    args = parser().parse_args()
    started = monotonic()
    mode = "export only" if args.command == "export" else "apply" if args.apply else "dry-run; no target writes"
    progress(f"Starting {args.command} ({mode}; authentication: {args.auth_mode}).")
    try:
        run_migration(args)
        progress(f"Run completed in {monotonic() - started:.1f} seconds.", "DONE")
        return 0
    except KeyboardInterrupt:
        progress("Run interrupted. If apply had started, inspect the action journal before retrying.", "ERROR", error=True)
        return 130
    except (MigrationError, OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        progress(f"Run failed after {monotonic() - started:.1f} seconds: {error}", "ERROR", error=True)
        return 1


def run_migration(args: argparse.Namespace) -> None:
    """Validate options, prepare artifacts, authenticate, export, then preflight/apply."""
    if args.api_version and not re.fullmatch(r"[0-9]+\.[0-9]+", args.api_version):
        raise MigrationError("--api-version must use the format 68.0.")
    if args.command in ("import", "migrate") and bool(args.update_existing) != bool(args.target_version):
        raise MigrationError("--update-existing and --target-version must be supplied together.")
    roles = {}
    if args.command in ("export", "migrate"):
        if not args.output_dir:
            raise MigrationError("--output-dir must be a nonempty parent directory.")
        roles["source"] = args.source_org
    if args.command in ("import", "migrate"):
        roles["target"] = args.target_org
    if len(roles) == 2 and roles["source"] == roles["target"]:
        raise MigrationError("Use distinct source and target org aliases.")
    settings = {role: jwt_settings(role) for role in roles} if args.auth_mode == "jwt" else {}
    directory = prepare_run_directory(args)
    if args.command == "import":
        manifest, _ = load_export(directory)
        load_mappings(args.mapping_file, manifest["resources"])
    for role, alias in roles.items():
        if args.auth_mode == "jwt":
            login_jwt(alias, role, settings[role])
    source = Org(args.source_org, args.api_version) if "source" in roles else None
    target = Org(args.target_org, args.api_version) if "target" in roles else None
    if source and target and source.org_id[:15] == target.org_id[:15]:
        raise MigrationError("Source and target resolve to the same org.")
    if source:
        export_model(source, args, directory)
    if target:
        import_model(target, args, directory)


# Common query, validation, checksum, and artifact-file helpers.
def escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def one(records: list[dict], label: str) -> dict:
    if len(records) != 1:
        raise MigrationError(f"{label}: expected exactly one match, found {len(records)}.")
    return records[0]


def checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MigrationError(f"Cannot read {path} as a UTF-8 JSON object.") from error
    if not isinstance(value, dict):
        raise MigrationError(f"{path} must contain a JSON object.")
    return value


# Validate CML content and every catalog reference before using an export.
def load_export(directory: Path) -> tuple[dict, bytes]:
    manifest = read_json_object(directory / "manifest.json")
    content = (directory / "model.cml").read_bytes()
    if manifest.get("schema_version") != 1 or checksum(content) != manifest.get("cml_sha256"):
        raise MigrationError("Unsupported manifest version or CML checksum mismatch. Re-export the source model.")
    if not content.strip():
        raise MigrationError("The exported CML is empty.")
    try:
        content.decode("utf-8")
    except UnicodeError as error:
        raise MigrationError("The exported CML must be UTF-8 text.") from error
    for name in ("model_api", "source_org_id", "context_definition", "product_key"):
        if not isinstance(manifest.get(name), str) or not manifest[name].strip():
            raise MigrationError(f"Manifest requires a nonempty {name} string.")
    record_id(manifest["source_org_id"])
    resources = manifest.get("resources")
    rows = manifest.get("associations")
    if not isinstance(resources, dict) or not isinstance(rows, list):
        raise MigrationError("Manifest resources must be an object and associations must be a list.")
    for identifier, resource in resources.items():
        record_id(identifier)
        if not isinstance(resource, dict) or not isinstance(resource.get("identity"), dict):
            raise MigrationError(f"Invalid resource descriptor for {identifier}.")
        object_name, identity = resource.get("object"), resource["identity"]
        references = []
        if object_name == "Product2":
            if not isinstance(identity.get("external_id"), str) or not identity["external_id"].strip():
                raise MigrationError(f"Product resource {identifier} has no nonempty external ID.")
        elif object_name in ("ProductClassification", "ProductRelationshipType", "ProductComponentGroup"):
            if not isinstance(identity.get("name"), str) or not identity["name"].strip():
                raise MigrationError(f"Resource {identifier} requires a nonempty name.")
            if object_name == "ProductComponentGroup":
                references = [("parent", "Product2", True), ("group", "ProductComponentGroup", False)]
        elif object_name == "ProductRelatedComponent":
            if bool(identity.get("child")) == bool(identity.get("classification")):
                raise MigrationError(f"Component {identifier} requires exactly one child product/classification.")
            references = [("parent", "Product2", True), ("child", "Product2", False),
                          ("classification", "ProductClassification", False),
                          ("group", "ProductComponentGroup", False),
                          ("relationship", "ProductRelationshipType", False)]
        else:
            raise MigrationError(f"Unsupported manifest resource object for {identifier}.")
        for name, expected_object, required in references:
            if name not in identity:
                raise MigrationError(f"Resource {identifier} is missing its {name} identity.")
            reference = identity[name]
            if reference is None and not required:
                continue
            record_id(reference)
            linked = resources.get(reference)
            if not isinstance(linked, dict) or linked.get("object") != expected_object:
                raise MigrationError(f"Resource {identifier} has an invalid {name} reference.")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("tag"), str) or not row["tag"].strip():
            raise MigrationError("Each association requires a nonempty tag string.")
        reference = record_id(row.get("reference"))
        object_name = resources.get(reference, {}).get("object")
        valid_type = row.get("tag_type") == "Type" and object_name in ("Product2", "ProductClassification")
        valid_port = row.get("tag_type") == "Port" and object_name == "ProductRelatedComponent"
        if not (valid_type or valid_port):
            raise MigrationError("Association tag type does not match its catalog reference.")
    return manifest, content


# Accept explicit mappings only for resources present in the export.
def load_mappings(path: str | None, resources: dict) -> dict:
    mappings = read_json_object(Path(path)) if path else {}
    for object_name, values in mappings.items():
        if not isinstance(values, dict):
            raise MigrationError("Mappings must contain object names mapped to source-ID/target-ID dictionaries.")
        for source_id, target_id in values.items():
            record_id(source_id)
            record_id(target_id)
            resource = resources.get(source_id)
            if not resource or resource["object"] != object_name:
                raise MigrationError(f"Mapping for {source_id} does not match an exported {object_name} resource.")
    return mappings


# Give each invocation its own labeled folder without modifying earlier runs.
def prepare_run_directory(args: argparse.Namespace) -> Path:
    input_directory = Path(args.input_dir) if args.command == "import" else None
    if input_directory:
        for name in ("model.cml", "manifest.json"):
            if not (input_directory / name).is_file():
                raise MigrationError(f"Import requires {name} in {input_directory}. Use the exported run folder as --input-dir.")
    base = Path(args.output_dir) if args.output_dir else input_directory.parent
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    mode = "only" if args.command == "export" else "apply" if args.apply else "dryrun"
    directory = Path(mkdtemp(prefix=f"run-{args.command}-{mode}-{stamp}-", dir=base))
    progress(f"Run folder: {directory.resolve()}")
    if input_directory:
        for name in ("model.cml", "manifest.json"):
            shutil.copy2(input_directory / name, directory / name)
        progress(f"Copied selected export from {input_directory} into this run folder.")
    if getattr(args, "mapping_file", None):
        mapping_copy = directory / "mappings.json"
        shutil.copy2(args.mapping_file, mapping_copy)
        args.mapping_file = str(mapping_copy)
    return directory


# Validate record IDs and capture CLI responses without exposing credentials.
def record_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?", value):
        raise MigrationError("Invalid Salesforce record ID in selection or mapping.")
    return value


def sf_json(arguments: list[str]) -> dict:
    executable = shutil.which("sf")
    if not executable:
        raise MigrationError("Salesforce CLI (sf) is not on PATH.")
    try:
        result = subprocess.run(
            [executable, *arguments, "--json"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise MigrationError("Salesforce CLI failed or returned invalid JSON; check org authentication.") from error
    if not isinstance(payload, dict):
        raise MigrationError("Salesforce CLI returned an invalid response object.")
    if result.returncode or payload.get("status") != 0:
        raise MigrationError("Salesforce CLI command failed; check the org alias, authentication, and permissions.")
    if not isinstance(payload.get("result"), dict):
        raise MigrationError("Salesforce CLI returned an invalid result object.")
    return payload["result"]


# Optional pipeline authentication using role-specific JWT settings.
def jwt_settings(role: str) -> dict[str, str]:
    prefix = f"SF_{role.upper()}_"
    settings = {}
    for name in ("USERNAME", "CLIENT_ID", "JWT_KEY_FILE", "INSTANCE_URL"):
        value = os.environ.get(prefix + name, "").strip()
        if not value or any(character in value for character in "\r\n"):
            raise MigrationError(f"JWT authentication requires a nonempty {prefix + name} environment variable.")
        settings[name] = value
    key_path = Path(settings["JWT_KEY_FILE"])
    if not key_path.is_file() or not os.access(key_path, os.R_OK):
        raise MigrationError(f"{prefix}JWT_KEY_FILE must identify a readable private-key file.")
    instance = urlsplit(settings["INSTANCE_URL"])
    if instance.scheme != "https" or not instance.hostname or instance.username or instance.password or instance.query or instance.fragment:
        raise MigrationError(f"{prefix}INSTANCE_URL must be an HTTPS Salesforce login or My Domain URL without credentials, query, or fragment.")
    return settings


def login_jwt(alias: str, role: str, settings: dict[str, str]) -> None:
    progress(f"Logging in to {role} org alias {alias} using JWT...", "AUTH")
    sf_json([
        "org", "login", "jwt", "--alias", alias,
        "--username", settings["USERNAME"],
        "--client-id", settings["CLIENT_ID"],
        "--jwt-key-file", settings["JWT_KEY_FILE"],
        "--instance-url", settings["INSTANCE_URL"],
    ])
    progress(f"JWT login completed for {role} org alias {alias}.", "AUTH")


# Authenticated REST access, schema checks, and a read-only-by-default write guard.
class Org:
    def __init__(self, alias: str, api_version: str | None, allow_writes: bool = False):
        progress(f"Authenticating {alias} through Salesforce CLI...", "AUTH")
        auth = sf_json(["org", "display", "--target-org", alias])
        self.alias = alias
        self.instance = auth["instanceUrl"].rstrip("/")
        token_result = sf_json(["org", "auth", "show-access-token", "--target-org", alias])
        self.token = token_result["accessToken"]
        if not isinstance(self.token, str) or not self.token or any(character.isspace() for character in self.token):
            raise MigrationError("Salesforce CLI did not return a usable access token. Check CLI compatibility and org authentication.")
        self.org_id = auth["id"]
        self.allow_writes = allow_writes
        self.opener = build_opener(NoRedirect())
        self.schemas: dict[str, dict] = {}
        if urlsplit(self.instance).scheme != "https":
            raise MigrationError("The authenticated org must use HTTPS.")
        progress(f"{alias}: checking supported API versions...", "AUTH")
        versions = self.request("GET", "/services/data/")
        available = {item["version"] for item in versions}
        self.version = api_version or max(available, key=float)
        if self.version not in available:
            raise MigrationError(f"API version {self.version} is not supported by {alias}.")
        self.root = f"/services/data/v{self.version}"
        progress(f"Connected to {alias}, API {self.version}; writes {'enabled' if allow_writes else 'disabled'}.", "AUTH")

    def request(self, method: str, path: str, body=None, raw: bool = False):
        if method != "GET" and not self.allow_writes:
            raise MigrationError("Org writes are disabled. Use --apply explicitly.")
        url = path if path.startswith("https://") else self.instance + path
        parts = urlsplit(url)
        origin = urlsplit(self.instance)
        if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc) or not parts.path.startswith("/services/data/"):
            raise MigrationError("Refusing an API link outside the authenticated Salesforce origin.")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "*/*" if raw else "application/json",
        })
        try:
            with self.opener.open(request, timeout=60) as response:
                content = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").replace(self.token, "[REDACTED]")
            raise MigrationError(f"{self.alias}: {method} {parts.path} failed (HTTP {error.code}): {detail[:1200]}") from None
        except (URLError, OSError) as error:
            raise MigrationError(f"{self.alias}: {method} request failed. Writes are not automatically retried.") from error
        if raw:
            return content
        return json.loads(content) if content else None

    def describe(self, object_name: str) -> dict:
        if object_name not in self.schemas:
            progress(f"{self.alias}: checking {object_name} schema...", "SCHEMA")
            self.schemas[object_name] = self.request("GET", f"{self.root}/sobjects/{object_name}/describe")
        return self.schemas[object_name]

    def field(self, object_name: str, name: str, required: bool = True) -> str | None:
        fields = self.describe(object_name)["fields"]
        found = next((item["name"] for item in fields if item["name"].lower() == name.lower()), None)
        if required and not found:
            raise MigrationError(f"{self.alias}: {object_name}.{name} is not accessible in describe.")
        return found

    def writable(self, object_name: str, names: list[str], operation: str = "createable") -> None:
        schema = self.describe(object_name)
        if not schema.get(operation):
            raise MigrationError(f"{self.alias}: {object_name} is not {operation}; configure it through the supported UI first.")
        for name in names:
            actual = self.field(object_name, name)
            info = next(item for item in schema["fields"] if item["name"] == actual)
            if not info.get(operation):
                raise MigrationError(f"{self.alias}: {object_name}.{actual} is not {operation}.")

    def query(self, statement: str) -> list[dict]:
        page = self.request("GET", f"{self.root}/query?{urlencode({'q': statement})}")
        records = list(page["records"])
        while not page["done"]:
            progress(f"{self.alias}: retrieving next query page ({len(records)} records read)...", "READ")
            page = self.request("GET", page["nextRecordsUrl"])
            records.extend(page["records"])
        return records

    def find(self, object_name: str, filters: dict) -> list[dict]:
        clauses = []
        for name, value in filters.items():
            actual = self.field(object_name, name)
            clauses.append(f"{actual} = null" if value is None else f"{actual} = '{escaped(str(value))}'")
        return self.query(f"SELECT Id FROM {object_name} WHERE {' AND '.join(clauses)}")

    def get(self, object_name: str, identifier: str, names: list[str]) -> dict:
        actual = [self.field(object_name, name) for name in names]
        selected = ", ".join(dict.fromkeys(["Id", *actual]))
        return one(self.query(f"SELECT {selected} FROM {object_name} WHERE Id = '{record_id(identifier)}'"), object_name)

    def create(self, object_name: str, values: dict) -> str:
        self.writable(object_name, list(values))
        result = self.request("POST", f"{self.root}/sobjects/{object_name}", values)
        if not result or not result.get("success") or result.get("errors"):
            raise MigrationError(f"{object_name} creation failed: {result}")
        return record_id(result["id"])

    def blob(self, version_id: str) -> bytes:
        return self.request("GET", f"{self.root}/sobjects/ExpressionSetDefinitionVersion/{record_id(version_id)}/ConstraintModel", raw=True)


# Resolve model/version ownership, activation state, and association records.
def resolve_model(org: Org, api_name: str) -> tuple[dict | None, dict | None]:
    models = org.find("ExpressionSet", {"ApiName": api_name})
    if not models:
        return None, None
    model = one(models, f"Model {api_name}")
    usage = org.get("ExpressionSet", model["Id"], ["UsageType"])
    if usage.get("UsageType") != "Constraint":
        raise MigrationError(f"{api_name} exists but is not a constraint model.")
    definition = one(org.find("ExpressionSetDefinition", {"DeveloperName": api_name}), f"Definition {api_name}")
    return model, definition


def select_version(org: Org, definition_id: str, selector: str | None) -> dict:
    object_name = "ExpressionSetDefinitionVersion"
    parent = org.field(object_name, "ExpressionSetDefinitionId")
    optional = [org.field(object_name, name, False) for name in ("VersionNumber", "Status", "IsActive", "LastModifiedDate")]
    columns = ", ".join(["Id", *[name for name in optional if name]])
    versions = org.query(f"SELECT {columns} FROM {object_name} WHERE {parent} = '{record_id(definition_id)}'")
    candidates = versions
    if selector:
        candidates = [item for item in versions if item["Id"] == selector or item["Id"][:15] == selector
                      or (item.get("VersionNumber") is not None and str(item["VersionNumber"]) == selector)]
    if len(candidates) != 1:
        choices = ", ".join(f"{item['Id']} (number={item.get('VersionNumber', 'unknown')})" for item in versions)
        raise MigrationError(f"Expected one definition version, found {len(candidates)} matches. "
                             f"Use --source-version/--target-version with a definition-version ID or number. Available: {choices or 'none'}")
    return candidates[0]


def require_inactive(org: Org, version: dict) -> None:
    state_known = False
    if "Status" in version:
        if version["Status"] not in ("Draft", "Inactive"):
            raise MigrationError(f"Target definition version must be Draft or Inactive, not {version['Status']}.")
        state_known = True
    elif "IsActive" in version:
        if version["IsActive"] is not False:
            raise MigrationError("Target definition version is active or its state is unknown.")
        state_known = True
    runtime = org.find("ExpressionSetVersion", {"ExpressionSetDefinitionVerId": version["Id"]})
    for item in runtime:
        state = org.get("ExpressionSetVersion", item["Id"], ["IsActive"])
        if state["IsActive"] is not False:
            raise MigrationError("The corresponding target ExpressionSetVersion is active. Deactivate through the supported UI first.")
        state_known = True
    if not state_known:
        raise MigrationError("Cannot verify target version state; no changes will be made.")


def context_links(org: Org, definition_id: str) -> list[dict]:
    object_name = "ExpressionSetDefinitionContextDefinition"
    for name in ("ExpressionSetDefinitionId", "ContextDefinitionId"):
        org.field(object_name, name)
    return org.query(f"SELECT Id, ContextDefinitionId FROM {object_name} WHERE ExpressionSetDefinitionId = '{record_id(definition_id)}'")


def associations(org: Org, model_id: str) -> list[dict]:
    object_name = "ExpressionSetConstraintObj"
    for name in ("ExpressionSetId", "ConstraintModelTag", "ConstraintModelTagType", "ReferenceObjectId"):
        org.field(object_name, name)
    return org.query(f"SELECT Id, ConstraintModelTag, ConstraintModelTagType, ReferenceObjectId FROM {object_name} WHERE ExpressionSetId = '{record_id(model_id)}'")


# Capture portable catalog identities instead of carrying source lookup IDs across orgs.
class Catalog:
    def __init__(self, org: Org, product_key: str):
        self.org = org
        self.product_key = org.field("Product2", product_key)
        self.resources: dict[str, dict] = {}
        self.visiting: set[str] = set()

    def capture(self, object_name: str, identifier: str | None) -> str | None:
        if not identifier:
            return None
        record_id(identifier)
        if identifier in self.visiting:
            raise MigrationError("Cycle found in catalog component-group ancestry.")
        if identifier in self.resources:
            return identifier
        self.visiting.add(identifier)
        progress(f"Reading {object_name} catalog identity ({len(self.resources)} collected)...", "EXPORT")
        if object_name == "Product2":
            row = self.org.get(object_name, identifier, ["Name", self.product_key])
            value = row[self.product_key]
            if not isinstance(value, str) or not value.strip():
                raise MigrationError(f"Product '{row['Name']}' has no {self.product_key}; populate it before exporting.")
            one(self.org.find(object_name, {self.product_key: value}), f"Source product key {value}")
            identity = {"external_id": value, "name": row["Name"]}
        elif object_name in ("ProductClassification", "ProductRelationshipType"):
            row = self.org.get(object_name, identifier, ["Name"])
            identity = {"name": row["Name"]}
        elif object_name == "ProductComponentGroup":
            row = self.org.get(object_name, identifier, ["Name", "ParentProductId", "ParentGroupId"])
            identity = {"name": row["Name"],
                        "parent": self.capture("Product2", row["ParentProductId"]),
                        "group": self.capture(object_name, row["ParentGroupId"])}
        elif object_name == "ProductRelatedComponent":
            required = ["ParentProductId", "ChildProductId", "ChildProductClassificationId", "ProductComponentGroupId"]
            relationship = self.org.field(object_name, "ProductRelationshipTypeId", False)
            row = self.org.get(object_name, identifier, required + ([relationship] if relationship else []))
            if bool(row["ChildProductId"]) == bool(row["ChildProductClassificationId"]):
                raise MigrationError("Bundle component must reference exactly one child product or classification.")
            identity = {"parent": self.capture("Product2", row["ParentProductId"]),
                        "child": self.capture("Product2", row["ChildProductId"]),
                        "classification": self.capture("ProductClassification", row["ChildProductClassificationId"]),
                        "group": self.capture("ProductComponentGroup", row["ProductComponentGroupId"]),
                        "relationship": self.capture("ProductRelationshipType", row.get(relationship)) if relationship else None}
        else:
            raise MigrationError(f"Unsupported association target: {object_name}.")
        self.resources[identifier] = {"object": object_name, "identity": identity}
        self.visiting.remove(identifier)
        return identifier


def reference_type(org: Org, identifier: str) -> str:
    prefix = record_id(identifier)[:3]
    matches = [name for name in ("Product2", "ProductClassification", "ProductRelatedComponent")
               if org.describe(name).get("keyPrefix") == prefix]
    return one([{"name": name} for name in matches], "Association object type")["name"]


# Export the selected CML version, context binding, and catalog associations.
def export_model(org: Org, args: argparse.Namespace, directory: Path) -> Path:
    progress(f"Resolving source model {args.model_api} in {org.alias}...", "EXPORT")
    model, definition = resolve_model(org, args.model_api)
    if not model or not definition:
        raise MigrationError(f"Source model {args.model_api} was not found.")
    version = select_version(org, definition["Id"], args.source_version)
    progress(f"Downloading CML from definition version {version['Id']}...", "EXPORT")
    content = org.blob(version["Id"])
    if not content.strip():
        raise MigrationError("Selected source version has no CML content.")
    content.decode("utf-8")
    progress(f"Downloaded {len(content)} CML bytes; reading context binding...", "EXPORT")
    link = one(context_links(org, definition["Id"]), "Source context binding")
    if not link["ContextDefinitionId"]:
        raise MigrationError("File-based context bindings are not supported by this script.")
    context = org.get("ContextDefinition", link["ContextDefinitionId"], ["DeveloperName"])
    progress("Reading source model associations...", "EXPORT")
    rows = associations(org, model["Id"])
    progress(f"Found {len(rows)} associations; collecting catalog identities...", "EXPORT")
    catalog = Catalog(org, args.product_key)
    exported = []
    for index, row in enumerate(rows, start=1):
        progress(f"Association {index}/{len(rows)}: {row['ConstraintModelTagType']} {row['ConstraintModelTag']}", "EXPORT")
        object_name = reference_type(org, row["ReferenceObjectId"])
        tag_type = row["ConstraintModelTagType"]
        if tag_type not in ("Type", "Port") or (tag_type == "Port") != (object_name == "ProductRelatedComponent"):
            raise MigrationError(f"Unsupported association tag/target combination: {tag_type}/{object_name}.")
        exported.append({"tag": row["ConstraintModelTag"], "tag_type": tag_type,
                         "reference": catalog.capture(object_name, row["ReferenceObjectId"])})
    if any((directory / name).exists() for name in ("model.cml", "manifest.json")):
        raise MigrationError("Run folder unexpectedly contains a model; refusing to overwrite it.")
    manifest = {"schema_version": 1, "model_api": args.model_api, "source_org_id": org.org_id,
                "source_version": version, "context_definition": context["DeveloperName"],
                "product_key": catalog.product_key, "cml_sha256": checksum(content),
                "resources": catalog.resources, "associations": exported}
    progress(f"Writing export files to {directory}...", "EXPORT")
    (directory / "model.cml").write_bytes(content)
    write_json(directory / "manifest.json", manifest)
    progress(f"Exported {args.model_api}: {len(exported)} associations, {len(catalog.resources)} catalog references -> {directory}", "EXPORT")
    return directory


# Match exported identities to target records, rejecting missing or ambiguous matches.
class Resolver:
    def __init__(self, org: Org, manifest: dict, mappings: dict):
        self.org = org
        self.resources = manifest["resources"]
        self.key = org.field("Product2", manifest["product_key"])
        self.mappings = mappings
        self.resolved: dict[str, str] = {}
        self.visiting: set[str] = set()

    def resolve(self, source_id: str | None) -> str | None:
        if source_id is None:
            return None
        if source_id in self.resolved:
            return self.resolved[source_id]
        if source_id in self.visiting:
            raise MigrationError("Cycle in exported catalog references.")
        self.visiting.add(source_id)
        resource = self.resources.get(source_id)
        if not resource:
            raise MigrationError(f"Manifest reference {source_id} has no catalog identity.")
        object_name, identity = resource["object"], resource["identity"]
        progress(f"Resolving {object_name} in {self.org.alias} ({len(self.resolved)}/{len(self.resources)} references matched)...", "MATCH")
        if object_name == "Product2":
            if not isinstance(identity.get("external_id"), str) or not identity["external_id"].strip():
                raise MigrationError("Manifest product has no nonempty external ID.")
            filters = {self.key: identity["external_id"]}
        elif object_name in ("ProductClassification", "ProductRelationshipType"):
            filters = {"Name": identity["name"]}
        elif object_name == "ProductComponentGroup":
            filters = {"Name": identity["name"], "ParentProductId": self.resolve(identity["parent"]),
                       "ParentGroupId": self.resolve(identity["group"])}
        elif object_name == "ProductRelatedComponent":
            if not identity.get("parent") or bool(identity.get("child")) == bool(identity.get("classification")):
                raise MigrationError("Manifest component must have a parent and exactly one child product/classification.")
            filters = {"ParentProductId": self.resolve(identity["parent"]),
                       "ChildProductId": self.resolve(identity["child"]),
                       "ChildProductClassificationId": self.resolve(identity["classification"]),
                       "ProductComponentGroupId": self.resolve(identity["group"])}
            if identity["relationship"]:
                filters["ProductRelationshipTypeId"] = self.resolve(identity["relationship"])
        else:
            raise MigrationError(f"Unsupported manifest object: {object_name}.")
        override = self.mappings.get(object_name, {}).get(source_id)
        if override:
            row = self.org.get(object_name, record_id(override), list(filters))
            if object_name not in ("ProductClassification", "ProductRelationshipType"):
                if any(row.get(name) != value for name, value in filters.items()):
                    raise MigrationError(f"Explicit mapping for {source_id} conflicts with its product/group/component identity.")
            target_id = row["Id"]
        else:
            matches = self.org.find(object_name, filters)
            target_id = one(matches, f"Target {object_name} for source {source_id}; use --mapping-file for ambiguity")["Id"]
        self.resolved[source_id] = target_id
        self.visiting.remove(source_id)
        progress(f"Matched {object_name} ({len(self.resolved)}/{len(self.resources)} references).", "MATCH")
        return target_id


# Compare associations by tag, tag type, and resolved target reference.
def signature(rows: list[dict]) -> set[tuple]:
    return {(row["ConstraintModelTag"], row["ConstraintModelTagType"], row["ReferenceObjectId"]) for row in rows}


# Read-only target preflight; dry-run stops here before any configuration writes.
def import_model(org: Org, args: argparse.Namespace, directory: Path) -> None:
    progress(f"Reading exported model from {directory} and validating its checksum...", "PREFLIGHT")
    manifest, content = load_export(directory)
    if org.org_id[:15] == manifest["source_org_id"][:15]:
        raise MigrationError("Source and target resolve to the same org.")
    mappings = load_mappings(args.mapping_file, manifest["resources"])
    progress(f"Matching {len(manifest['associations'])} associations against target catalog records...", "MATCH")
    resolver = Resolver(org, manifest, mappings)
    desired = []
    for index, row in enumerate(manifest["associations"], start=1):
        progress(f"Association {index}/{len(manifest['associations'])}: {row['tag_type']} {row['tag']}", "MATCH")
        reference = row["reference"]
        tag_type = row["tag_type"]
        desired.append({"ConstraintModelTag": row["tag"], "ConstraintModelTagType": tag_type,
                        "ReferenceObjectId": resolver.resolve(reference)})
    if len(signature(desired)) != len(desired):
        raise MigrationError("Exported associations collapse to duplicate target associations.")
    api_name = args.target_model_api or manifest["model_api"]
    progress(f"Checking target model {api_name}, context, and version state...", "PREFLIGHT")
    model, definition = resolve_model(org, api_name)
    context_name = args.context_definition or manifest["context_definition"]
    context = one(org.find("ContextDefinition", {"DeveloperName": context_name}), f"Target context {context_name}")
    version = None
    existing = []
    links = []
    old_content = None
    if model:
        if not args.update_existing or not args.target_version:
            raise MigrationError("Target model exists. Use --update-existing and --target-version for an explicit inactive version.")
        version = select_version(org, definition["Id"], args.target_version)
        progress(f"Checking inactive target definition version {version['Id']}...", "PREFLIGHT")
        require_inactive(org, version)
        existing = associations(org, model["Id"])
        if len(signature(existing)) != len(existing):
            raise MigrationError("Target contains duplicate associations. Reconcile them manually before updating.")
        links = context_links(org, definition["Id"])
        old_content = org.blob(version["Id"])
    elif args.update_existing or args.target_version:
        raise MigrationError("Update flags supplied but the target model does not exist.")
    progress("Checking existing associations, context binding, and write permissions...", "PREFLIGHT")
    existing_signature = signature(existing)
    stale = existing_signature - signature(desired)
    if stale:
        raise MigrationError(f"Target has {len(stale)} stale associations. Reconcile them manually before updating; this script never deletes.")
    if links and (len(links) != 1 or links[0]["ContextDefinitionId"] != context["Id"]):
        raise MigrationError("Target context binding differs. Configure it manually before importing.")
    if not links:
        org.writable("ExpressionSetDefinitionContextDefinition", ["ExpressionSetDefinitionId", "ContextDefinitionId"])
    creation = {"ApiName": api_name, "Name": api_name, **CONSTRAINT_MODEL_DEFAULTS}
    if not model:
        org.writable("ExpressionSet", list(creation))
    content_changed = old_content != content
    if content_changed:
        org.writable("ExpressionSetDefinitionVersion", ["ConstraintModel"], "updateable")
    missing = [row for row in desired if (row["ConstraintModelTag"], row["ConstraintModelTagType"], row["ReferenceObjectId"]) not in existing_signature]
    if missing:
        org.writable("ExpressionSetConstraintObj", ["ExpressionSetId", "ConstraintModelTag", "ConstraintModelTagType", "ReferenceObjectId"])
    for index, row in enumerate(desired, start=1):
        progress(f"Checking model ownership conflicts {index}/{len(desired)}...", "PREFLIGHT")
        conflicts = org.find("ExpressionSetConstraintObj", {"ReferenceObjectId": row["ReferenceObjectId"]})
        for conflict in conflicts:
            owner = org.get("ExpressionSetConstraintObj", conflict["Id"], ["ExpressionSetId"])["ExpressionSetId"]
            if not model or owner != model["Id"]:
                raise MigrationError("A target catalog reference belongs to another model. Review its associations before importing.")
    report = {"target_org_id": org.org_id, "target_model_api": api_name, "context_definition": context_name,
              "target_version": version, "create_model": model is None, "association_count": len(desired),
              "upload_cml": content_changed, "create_context_binding": not links,
              "insert_associations": missing, "resolved_references": resolver.resolved,
              "cml_sha256": checksum(content), "dry_run": not args.apply}
    write_json(directory / "preflight.json", report)
    progress(f"{'APPLY' if args.apply else 'DRY RUN'}: {api_name} -> {org.alias}; {len(desired)} associations, {len(missing)} inserts.", "PREFLIGHT")
    progress(f"Preflight report: {directory / 'preflight.json'}", "PREFLIGHT")
    if not args.apply:
        progress("No target-org writes performed. Re-run with --apply to import.", "DONE")
        return
    if model and not missing and links and not content_changed:
        progress("Target already matches the export. No org writes needed.", "DONE")
        return
    progress("Checking target environment before applying changes...", "APPLY")
    sandbox = one(org.query("SELECT IsSandbox, OrganizationType FROM Organization"), "Target org environment")
    if not sandbox["IsSandbox"] and sandbox["OrganizationType"] != "Developer Edition" and not args.allow_production:
        raise MigrationError("Production target requires --allow-production in addition to --apply.")
    plan = ImportPlan(api_name=api_name, context=context, model=model, definition=definition,
                      version=version, existing=existing, links=links, old_content=old_content,
                      content=content, missing=missing, desired=desired, directory=directory)
    apply_import(org, plan)


# Back up, recheck, and apply the approved plan while journaling partial changes.
def apply_import(org: Org, plan: ImportPlan) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = plan.directory / f"target-backup-{stamp}"
    progress(f"Saving target backup and action journal to {backup}...", "APPLY")
    backup.mkdir()
    if plan.old_content is not None:
        (backup / "model.cml").write_bytes(plan.old_content)
    write_json(backup / "snapshot.json", {"model": plan.model, "definition": plan.definition, "version": plan.version,
                                         "context_links": plan.links, "associations": plan.existing})
    journal = {"status": "started", "target_org_id": org.org_id, "model_api": plan.api_name, "actions": []}
    journal_path = backup / "journal.json"
    write_json(journal_path, journal)
    org.allow_writes = True

    def logged(action: str, identifier: str) -> None:
        journal["actions"].append({"action": action, "id": identifier})
        write_json(journal_path, journal)

    try:
        if plan.model:
            progress("Rechecking target state for concurrent changes...", "APPLY")
            current = select_version(org, plan.definition["Id"], plan.version["Id"])
            require_inactive(org, current)
            current_associations = associations(org, plan.model["Id"])
            if (current != plan.version or org.blob(plan.version["Id"]) != plan.old_content
                or len(current_associations) != len(plan.existing)
                or signature(current_associations) != signature(plan.existing)
                    or context_links(org, plan.definition["Id"]) != plan.links):
                raise MigrationError("Target changed after preflight. Re-run to inspect the new state.")
        else:
            if org.find("ExpressionSet", {"ApiName": plan.api_name}):
                raise MigrationError("Target model appeared after preflight. Re-run.")
            progress(f"Creating target constraint model {plan.api_name}...", "APPLY")
            identifier = org.create("ExpressionSet", {
                "ApiName": plan.api_name, "Name": plan.api_name, **CONSTRAINT_MODEL_DEFAULTS})
            logged("create ExpressionSet", identifier)
            plan.model, plan.definition = resolve_model(org, plan.api_name)
            if not plan.model or not plan.definition or plan.model["Id"] != identifier:
                raise MigrationError("New model's backing definition was not created as expected.")
            plan.version = select_version(org, plan.definition["Id"], None)
            require_inactive(org, plan.version)
            plan.links = context_links(org, plan.definition["Id"])
            if plan.links and (len(plan.links) != 1 or plan.links[0]["ContextDefinitionId"] != plan.context["Id"]):
                raise MigrationError("Auto-generated context binding differs from the requested context.")
        if not plan.links:
            progress("Creating target context binding...", "APPLY")
            identifier = org.create("ExpressionSetDefinitionContextDefinition", {
                "ExpressionSetDefinitionId": plan.definition["Id"], "ContextDefinitionId": plan.context["Id"]})
            logged("create context binding", identifier)
        if plan.content_changed:
            progress(f"Uploading {len(plan.content)} CML bytes to definition version {plan.version['Id']}...", "APPLY")
            org.request("PATCH", f"{org.root}/sobjects/ExpressionSetDefinitionVersion/{plan.version['Id']}",
                        {"ConstraintModel": base64.b64encode(plan.content).decode("ascii")})
            logged("upload ConstraintModel", plan.version["Id"])
            progress("CML upload completed.", "APPLY")
        for index, row in enumerate(plan.missing, start=1):
            progress(f"Creating association {index}/{len(plan.missing)}: {row['ConstraintModelTagType']} {row['ConstraintModelTag']}", "APPLY")
            identifier = org.create("ExpressionSetConstraintObj", {"ExpressionSetId": plan.model["Id"], **row})
            logged("create ExpressionSetConstraintObj", identifier)
        final_version = verify_import(org, plan)
        journal.update(status="verified", target_version=final_version)
        write_json(journal_path, journal)
        progress(f"Verified CML bytes, associations, and context. Model remains inactive. Backup/journal: {backup}", "DONE")
    except KeyboardInterrupt:
        journal["status"] = "interrupted; inspect recorded actions and target state before retrying"
        write_json(journal_path, journal)
        progress(f"Apply interrupted; partial changes may exist. Inspect {journal_path}.", "ERROR", error=True)
        raise
    except Exception as error:
        journal["status"] = "failed; inspect recorded actions before retrying"
        write_json(journal_path, journal)
        raise MigrationError(f"Apply failed; changes may be partial. No automatic rollback. Inspect {journal_path}. {error}") from error
    finally:
        org.allow_writes = False


# Read back CML, associations, context, and version state before reporting success.
def verify_import(org: Org, plan: ImportPlan) -> dict:
    progress("Verifying target CML readback...", "VERIFY")
    if org.blob(plan.version["Id"]) != plan.content:
        raise MigrationError("Target CML readback differs from the exported bytes.")
    progress("Verifying target associations and context binding...", "VERIFY")
    actual_associations = associations(org, plan.model["Id"])
    if len(actual_associations) != len(plan.desired) or signature(actual_associations) != signature(plan.desired):
        raise MigrationError("Target association readback differs from the migration manifest.")
    actual_links = context_links(org, plan.definition["Id"])
    if len(actual_links) != 1 or actual_links[0]["ContextDefinitionId"] != plan.context["Id"]:
        raise MigrationError("Target context binding failed readback verification.")
    progress("Verifying target version remains inactive...", "VERIFY")
    final_version = select_version(org, plan.definition["Id"], plan.version["Id"])
    require_inactive(org, final_version)
    return final_version


# Run only when invoked as a script, not when imported.
if __name__ == "__main__":
    sys.exit(main())