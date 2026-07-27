import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from .base import Method, Unit
from .models import MintInfoContact, MintInfoProtectedEndpoint, Nut15MppSupport
from .nuts.nuts import BLIND_AUTH_NUT, CLEAR_AUTH_NUT, MPP_NUT, WEBSOCKETS_NUT


def _match_protected_endpoint(endpoint_path: str, request_path: str) -> bool:
    """
    Match a request path against a protected endpoint path using wildcard rules.

    Rules (per NUT-21/NUT-22):
    1. Exact match: no trailing '*' -> request path MUST equal endpoint_path
    2. Prefix match: ends with '*' -> request path MUST start with the prefix ('*' removed)

    The '*' wildcard, if present, MUST be the final character only.
    """
    if endpoint_path.endswith("*"):
        # Prefix match: path must start with the prefix (excluding the trailing '*')
        prefix = endpoint_path[:-1]  # Remove the trailing '*'
        return request_path.startswith(prefix)
    else:
        # Exact match
        return request_path == endpoint_path


# NUT #06: This endpoint returns information about the mint that a wallet can show
# to the user and use to make decisions on how to interact with the mint.
class MintInfo(BaseModel):
    # NUT #06: `name` is the name of the mint and should be recognizable.
    name: Optional[str]
    # NUT #06: `pubkey` is the hex pubkey of the mint.
    pubkey: Optional[str]
    # NUT #06: `version` is the implementation name and the version of the software
    # running on this mint separated with a slash "/".
    version: Optional[str]
    # NUT #06: `description` is a short description of the mint that can be shown in
    # the wallet next to the mint's name.
    description: Optional[str]
    # NUT #06: `description_long` is a long description that can be shown in an
    # additional field.
    description_long: Optional[str]
    # NUT #06: `contact` is an array of contact objects to reach the mint operator.
    # A contact object consists of two fields. The `method` field denotes the contact
    # method (like "email"), the `info` field denotes the identifier (like "contact@me.com").
    contact: Optional[List[MintInfoContact]]
    # NUT #06: `motd` is the message of the day that the wallet must display to the
    # user. It should only be used to display important announcements to users, such
    # as scheduled maintenances.
    motd: Optional[str]
    # NUT #06: `icon_url` is the URL pointing to an image to be used as an icon for
    # the mint. Recommended to be squared in shape.
    icon_url: Optional[str]
    # NUT #06: `urls` is the list of endpoint URLs where the mint is reachable from.
    urls: Optional[List[str]]
    # NUT #06: `tos_url` is the URL pointing to the Terms of Service of the mint.
    tos_url: Optional[str]
    # NUT #06: `time` is the current time set on the server. The value is passed as a
    # Unix timestamp integer.
    time: Optional[int]
    # NUT #06: `nuts` indicates each NUT specification that the mint supports and its
    # settings. The settings are defined in each NUT separately.
    nuts: Dict[int, Any]

    def __str__(self):
        return f"{self.name} ({self.description})"

    @classmethod
    def from_json_str(cls, json_str: str):
        return cls.model_validate(json.loads(json_str))

    def supports_nut(self, nut: int) -> bool:
        if self.nuts is None:
            return False
        return nut in self.nuts

    def supports_mpp(self, method: str, unit: Unit) -> bool:
        if not self.nuts:
            return False
        nut_15 = self.nuts.get(MPP_NUT)
        if not nut_15 or not self.supports_nut(MPP_NUT) or not nut_15.get("methods"):
            return False

        for entry in nut_15["methods"]:
            entry_obj = Nut15MppSupport.model_validate(entry)
            if entry_obj.method == method and entry_obj.unit == unit.name:
                return True

        return False

    def supports_websocket_mint_quote(self, method: Method, unit: Unit) -> bool:
        if not self.nuts or not self.supports_nut(WEBSOCKETS_NUT):
            return False
        websocket_settings = self.nuts[WEBSOCKETS_NUT]
        if not websocket_settings or "supported" not in websocket_settings:
            return False
        websocket_supported = websocket_settings["supported"]
        for entry in websocket_supported:
            if entry["method"] == method.name and entry["unit"] == unit.name:
                if "bolt11_mint_quote" in entry["commands"]:
                    return True
        return False

    def requires_clear_auth(self) -> bool:
        return self.supports_nut(CLEAR_AUTH_NUT)

    def oidc_discovery_url(self) -> str:
        if not self.requires_clear_auth():
            raise Exception(
                "Could not get OIDC discovery URL. Mint info does not support clear auth."
            )
        return self.nuts[CLEAR_AUTH_NUT]["openid_discovery"]

    def oidc_client_id(self) -> str:
        if not self.requires_clear_auth():
            raise Exception(
                "Could not get client_id. Mint info does not support clear auth."
            )
        return self.nuts[CLEAR_AUTH_NUT]["client_id"]

    def required_clear_auth_endpoints(self) -> List[MintInfoProtectedEndpoint]:
        if not self.requires_clear_auth():
            return []
        return [
            MintInfoProtectedEndpoint.model_validate(e)
            for e in self.nuts[CLEAR_AUTH_NUT]["protected_endpoints"]
        ]

    def requires_clear_auth_path(self, method: str, path: str) -> bool:
        if not self.requires_clear_auth():
            return False
        path = "/" + path if not path.startswith("/") else path
        for endpoint in self.required_clear_auth_endpoints():
            if method == endpoint.method and _match_protected_endpoint(
                endpoint.path, path
            ):
                return True
        return False

    def requires_blind_auth(self) -> bool:
        return self.supports_nut(BLIND_AUTH_NUT)

    @property
    def bat_max_mint(self) -> int:
        if not self.requires_blind_auth():
            raise Exception(
                "Could not get max mint. Mint info does not support blind auth."
            )
        if not self.nuts[BLIND_AUTH_NUT].get("bat_max_mint"):
            raise Exception("Could not get max mint. bat_max_mint not set.")
        return self.nuts[BLIND_AUTH_NUT]["bat_max_mint"]

    def required_blind_auth_paths(self) -> List[MintInfoProtectedEndpoint]:
        if not self.requires_blind_auth():
            return []
        return [
            MintInfoProtectedEndpoint.model_validate(e)
            for e in self.nuts[BLIND_AUTH_NUT]["protected_endpoints"]
        ]

    def requires_blind_auth_path(self, method: str, path: str) -> bool:
        if not self.requires_blind_auth():
            return False
        path = "/" + path if not path.startswith("/") else path
        for endpoint in self.required_blind_auth_paths():
            if method == endpoint.method and _match_protected_endpoint(
                endpoint.path, path
            ):
                return True
        return False
