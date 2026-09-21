"""In-memory member data for the mock core-banking app. All values are fake."""

from __future__ import annotations

MEMBERS: dict[str, dict] = {
    "12345": {
        "name": "Ada Lovelace",
        "since": "03/14/2011",
        "ssn_last4": "1234",
        "phone": "(555) 010-2233",
        "accounts": [
            {"type": "Regular Share (Savings)", "number": "S-0001", "balance": "4,312.57"},
            {"type": "Share Draft (Checking)", "number": "S-0002", "balance": "812.10"},
        ],
    },
    "67890": {
        "name": "Grace Hopper",
        "since": "07/02/2008",
        "ssn_last4": "9876",
        "phone": "(555) 010-8899",
        "accounts": [
            {"type": "Regular Share (Savings)", "number": "S-0011", "balance": "15,004.00"},
        ],
    },
    "24680": {
        "name": "Katherine Johnson",
        "since": "11/30/2019",
        "ssn_last4": "5555",
        "phone": "(555) 010-4477",
        "accounts": [
            {"type": "Regular Share (Savings)", "number": "S-0021", "balance": "250.00"},
            {"type": "Money Market", "number": "S-0022", "balance": "9,800.42"},
        ],
    },
}

PRODUCTS = ["Regular Share (Savings)", "Share Draft (Checking)", "Money Market", "Holiday Club"]

# Tenant variants: same vendor product, different labels, one different route.
TENANTS: dict[str, dict] = {
    "alpha": {
        "brand": "Summit Federal Credit Union",
        "member_id_label": "Member ID",
        "search_route": "/members/search",
        "search_button": "Search",
        "open_sub_label": "Open sub-account",
        "confirm_button": "Open account",
    },
    "beta": {
        "brand": "Riverbend Community Bank",
        "member_id_label": "Member Number",
        "search_route": "/member/lookup",
        "search_button": "Find member",
        "open_sub_label": "Add share account",
        "confirm_button": "Create account",
    },
}
