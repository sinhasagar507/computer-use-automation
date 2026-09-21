"""Mock legacy core-banking app.

Deliberately hostile surface: frameset index, table-based layout, no ids, no test ids,
generic class names, server-rendered forms. Faults are injected with a cookie so a
replay can be pointed at a specific runtime condition.

Run:  uv run cua serve-mock [--tenant alpha|beta] [--port 5050]
"""

from __future__ import annotations

import os
import random
import time
import uuid

from flask import Flask, make_response, redirect, render_template, request, session

from mockbank.data import MEMBERS, PRODUCTS, TENANTS

FAULTS = {"none", "not_found", "validation", "denied", "dialog", "timeout", "slow", "error500"}


def create_app(tenant: str = "alpha") -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "mock-only-not-a-secret"
    cfg = TENANTS[tenant]
    app.config["TENANT"] = tenant
    app.config["USER"] = os.environ.get("MOCKBANK_USER", "teller1")
    app.config["PASSWORD"] = os.environ.get("MOCKBANK_PASSWORD", "demo-pass")

    @app.context_processor
    def inject():
        return {"t": cfg, "tenant": tenant, "fault": request.cookies.get("fault", "none")}

    def fault() -> str:
        return request.cookies.get("fault", "none")

    def logged_in() -> bool:
        return bool(session.get("user"))

    # --- fault control -------------------------------------------------------
    @app.route("/fault/<name>")
    def set_fault(name: str):
        if name not in FAULTS:
            return f"unknown fault {name}", 400
        resp = make_response(redirect("/"))
        if name == "none":
            resp.delete_cookie("fault")
        else:
            resp.set_cookie("fault", name)
        return resp

    # --- frameset shell ------------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/nav")
    def nav():
        return render_template("nav.html", logged_in=logged_in())

    @app.route("/main")
    def main():
        if not logged_in():
            return redirect("/login")
        return redirect(cfg["search_route"])

    # --- login ---------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            u = request.form.get("uid", "")
            p = request.form.get("pwd", "")
            if u == app.config["USER"] and p == app.config["PASSWORD"]:
                session["user"] = u
                return redirect(cfg["search_route"])
            error = "Invalid user ID or password."
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    # --- member search -------------------------------------------------------
    @app.route(cfg["search_route"], methods=["GET", "POST"])
    def search():
        if not logged_in():
            return redirect("/login")
        if fault() == "timeout":
            session.clear()
            return render_template("timeout.html"), 200
        if fault() == "slow":
            time.sleep(random.uniform(3, 6))
        if fault() == "error500" and request.method == "POST":
            return render_template("error500.html"), 500
        if request.method == "POST":
            mid = request.form.get("memberid", "").strip()
            if not mid.isdigit():
                return render_template("search.html", error="Member ID must be numeric.")
            if fault() == "not_found" or mid not in MEMBERS:
                return render_template("search.html", not_found=mid)
            return redirect(f"/members/{mid}")
        return render_template("search.html")

    # --- member detail -------------------------------------------------------
    @app.route("/members/<mid>")
    def member(mid: str):
        if not logged_in():
            return redirect("/login")
        if fault() == "denied":
            return render_template("denied.html"), 403
        m = MEMBERS.get(mid)
        if m is None:
            return render_template("search.html", not_found=mid)
        return render_template("member.html", mid=mid, m=m, show_dialog=(fault() == "dialog"))

    # --- open sub-account (multi-field form + confirmation) ------------------
    @app.route("/members/<mid>/subaccount/new", methods=["GET", "POST"])
    def subaccount_new(mid: str):
        if not logged_in():
            return redirect("/login")
        m = MEMBERS.get(mid)
        if m is None:
            return render_template("search.html", not_found=mid)
        errors: list[str] = []
        if request.method == "POST":
            product = request.form.get("product", "")
            nick = request.form.get("nickname", "").strip()
            dep = request.form.get("deposit", "").strip()
            if fault() == "validation" or not nick:
                errors.append("Nickname is required.")
            if product not in PRODUCTS:
                errors.append("Select a product.")
            try:
                if float(dep or "0") < 0:
                    errors.append("Deposit must be zero or more.")
            except ValueError:
                errors.append("Deposit must be a number.")
            if not errors:
                conf = f"CNF-{uuid.uuid4().hex[:8].upper()}"
                new_no = f"S-{random.randint(1000, 9999)}"
                m["accounts"].append({"type": product, "number": new_no, "balance": f"{float(dep or 0):,.2f}"})
                return render_template("confirm.html", mid=mid, m=m, product=product, nick=nick,
                                       deposit=dep or "0.00", conf=conf, new_no=new_no)
        return render_template("subaccount.html", mid=mid, m=m, products=PRODUCTS, errors=errors)

    return app


def run(tenant: str = "alpha", port: int = 5050, debug: bool = False) -> None:
    create_app(tenant).run(host="127.0.0.1", port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    run(os.environ.get("MOCKBANK_TENANT", "alpha"), int(os.environ.get("MOCKBANK_PORT", "5050")))
