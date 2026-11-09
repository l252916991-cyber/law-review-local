"""Conflict-of-interest checks: hits are found, block the case, and need a second person to clear.

Case A is an existing matter: the firm acts for 甲科技 against 乙贸易. A new case
B for 乙贸易 must stop until a partner other than the person who ran the check
approves it; a lawyer who is not on case A learns that a conflict exists, but
not which matter it is.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import IsolatedDatabaseTestCase

PARTNER_TOKEN = "conflict-partner-token-" + "x" * 24
SECOND_PARTNER_TOKEN = "conflict-partner2-token-" + "y" * 24
CREDIT = "91310000MA1FL5LX3X"
EVIDENCE = {"title": "收款凭证", "category": "书证", "fact": "收到款项", "quote": "收到款项", "status": "待复核"}


class ConflictCheckTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        from app.storage.db import init_db

        env = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({
                PARTNER_TOKEN: {"name": "风控合伙人", "admin": True},
                SECOND_PARTNER_TOKEN: {"name": "另一位合伙人", "admin": True},
            }),
        })
        env.start()
        cls.addClassCleanup(env.stop)
        init_db(seed=False)
        cls.client = TestClient(app)
        cls.partner = {"Authorization": f"Bearer {PARTNER_TOKEN}"}
        cls.partner2 = {"Authorization": f"Bearer {SECOND_PARTNER_TOKEN}"}
        cls.case_a = cls.new_case("甲科技诉乙贸易买卖合同纠纷", [
            {"role": "client", "name": "甲科技有限公司"},
            {"role": "opposing", "name": "乙贸易有限责任公司", "former_names": ["乙商贸有限公司"],
             "credit_code": CREDIT},
        ])["id"]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    @classmethod
    def new_case(cls, title: str, parties: list[dict], headers: dict | None = None) -> dict:
        response = cls.client.post("/api/cases", headers=headers or cls.partner,
                                   json={"title": title, "parties": parties})
        assert response.status_code == 201, response.text
        return response.json()

    def lawyer_on(self, case_id: int, name: str) -> dict:
        created = self.client.post("/api/members", headers=self.partner, json={
            "name": name, "assignments": [{"case_id": case_id, "role": "owner"}]})
        assert created.status_code == 201, created.text
        return {"Authorization": f"Bearer {created.json()['token']}"}

    def parties(self, case_id: int, headers: dict | None = None) -> dict:
        response = self.client.get(f"/api/cases/{case_id}/parties", headers=headers or self.partner)
        assert response.status_code == 200, response.text
        return response.json()

    def audit(self, case_id: int) -> list[str]:
        from contextlib import closing

        from app.storage.db import connect, system_access

        with system_access(), closing(connect()) as conn:
            return [row[0] for row in conn.execute("SELECT action FROM audit_log WHERE case_id = ? ORDER BY id",
                                                   (case_id,))]

    # -- matching ---------------------------------------------------------------
    def test_the_first_case_has_nothing_to_conflict_with(self):
        self.assertEqual(self.parties(self.case_a)["status"], "clear")

    def test_acting_for_the_other_side_is_a_direct_hit_despite_a_different_legal_form(self):
        case = self.new_case("乙贸易追讨货款", [{"role": "client", "name": "乙 贸易有限公司"}])
        conflict = case["conflict"]
        self.assertEqual(conflict["status"], "pending")
        hit = conflict["check"]["hits"][0]
        self.assertEqual((hit["kind"], hit["matched_on"], hit["case_id"]), ("direct", "name", self.case_a))
        self.assertEqual(hit["their_party"], {"role": "opposing", "name": "乙贸易有限责任公司"})
        self.assertIn("利益冲突检查", self.audit(case["id"]))

    def test_former_names_credit_codes_and_related_parties_are_matched(self):
        by_former = self.new_case("曾用名", [{"role": "client", "name": "乙商贸有限公司"}])
        self.assertEqual(by_former["conflict"]["check"]["hits"][0]["matched_on"], "former_name")
        by_code = self.new_case("改名后", [{"role": "client", "name": "丙物流有限公司", "credit_code": CREDIT.lower()}])
        self.assertEqual(by_code["conflict"]["check"]["hits"][0]["matched_on"], "credit_code")
        related = self.new_case("关联方", [{"role": "client", "name": "丁咨询有限公司"},
                                          {"role": "related", "name": "甲科技集团"}])
        self.assertEqual([hit["kind"] for hit in related["conflict"]["check"]["hits"]], ["related"])

    def test_the_same_role_on_both_sides_is_not_a_conflict(self):
        # Names used by no other test: earlier cases in this class act for 乙贸易.
        first = self.new_case("辛科技诉壬物业", [{"role": "client", "name": "辛科技有限公司"},
                                             {"role": "opposing", "name": "壬物业有限公司"}])
        self.assertEqual(first["conflict"]["status"], "clear")
        again = self.new_case("辛科技再诉壬物业", [{"role": "client", "name": "辛科技有限公司"},
                                               {"role": "opposing", "name": "壬物业有限公司"}])
        self.assertEqual(again["conflict"]["status"], "clear")
        self.assertEqual(again["conflict"]["check"]["hits"], [])

    def test_invalid_parties_are_rejected(self):
        for party in ({"role": "client", "name": "乙"}, {"role": "client", "name": "乙贸易", "credit_code": "123"},
                      {"role": "witness", "name": "乙贸易"}):
            response = self.client.post("/api/cases", headers=self.partner, json={"title": "无效", "parties": [party]})
            self.assertEqual(response.status_code, 422, party)

    # -- clearance ----------------------------------------------------------------
    def test_a_pending_case_cannot_be_worked_on_until_a_second_partner_clears_it(self):
        lawyer_case = self.new_case("乙贸易新委托", [])["id"]
        lawyer = self.lawyer_on(lawyer_case, "承办律师甲")
        put = self.client.put(f"/api/cases/{lawyer_case}/parties", headers=lawyer,
                              json={"parties": [{"role": "client", "name": "乙贸易有限公司"}]})
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(put.json()["status"], "pending")

        blocked = self.client.post(f"/api/cases/{lawyer_case}/evidence", headers=lawyer, json=EVIDENCE)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("合伙人放行前", blocked.json()["detail"])
        self.assertEqual(self.client.get(f"/api/cases/{lawyer_case}", headers=lawyer).status_code, 200)

        decision = {"decision": "approve", "reason": "已取得双方书面同意，另设信息隔离"}
        self.assertEqual(self.client.post(f"/api/cases/{lawyer_case}/conflict-check/decision",
                                          headers=lawyer, json=decision).status_code, 403)  # needs manage
        # The partner who re-runs the check cannot also clear it.
        self.client.post(f"/api/cases/{lawyer_case}/conflict-check", headers=self.partner2)
        own = self.client.post(f"/api/cases/{lawyer_case}/conflict-check/decision", headers=self.partner2, json=decision)
        self.assertEqual(own.status_code, 403)
        self.assertIn("发起人以外", own.json()["detail"])
        short = self.client.post(f"/api/cases/{lawyer_case}/conflict-check/decision", headers=self.partner,
                                 json={"decision": "approve", "reason": "同意"})
        self.assertEqual(short.status_code, 422)

        approved = self.client.post(f"/api/cases/{lawyer_case}/conflict-check/decision", headers=self.partner,
                                    json=decision)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual((approved.json()["status"], approved.json()["check"]["decided_by"]), ("approved", "风控合伙人"))
        self.assertEqual(self.client.post(f"/api/cases/{lawyer_case}/evidence", headers=lawyer,
                                          json=EVIDENCE).status_code, 201)
        self.assertIn("利益冲突放行", self.audit(lawyer_case))
        again = self.client.post(f"/api/cases/{lawyer_case}/conflict-check/decision", headers=self.partner,
                                 json=decision)
        self.assertEqual(again.status_code, 409)

    def test_a_rejected_case_stays_blocked_until_the_parties_change(self):
        case = self.new_case("驳回", [{"role": "client", "name": "乙贸易有限公司"}], headers=self.partner2)["id"]
        rejected = self.client.post(f"/api/cases/{case}/conflict-check/decision", headers=self.partner,
                                    json={"decision": "reject", "reason": "直接冲突且无法取得同意，不予受理"})
        self.assertEqual(rejected.json()["status"], "rejected")
        blocked = self.client.post(f"/api/cases/{case}/evidence", headers=self.partner, json=EVIDENCE)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("未获放行", blocked.json()["detail"])
        fixed = self.client.put(f"/api/cases/{case}/parties", headers=self.partner,
                                json={"parties": [{"role": "client", "name": "戊投资有限公司"}]})
        self.assertEqual(fixed.json()["status"], "clear")
        self.assertEqual(self.client.post(f"/api/cases/{case}/evidence", headers=self.partner,
                                          json=EVIDENCE).status_code, 201)

    # -- who sees what --------------------------------------------------------------
    def test_a_lawyer_not_on_the_other_case_learns_only_that_a_conflict_exists(self):
        case = self.new_case("仅知存在", [{"role": "client", "name": "乙贸易有限公司"}])["id"]
        lawyer = self.lawyer_on(case, "承办律师乙")
        hit = self.parties(case, lawyer)["check"]["hits"][0]
        self.assertEqual(hit, {"kind": "direct", "matched_on": "name", "restricted": True,
                               "our_party": {"role": "client", "name": "乙贸易有限公司"}})
        self.assertNotIn("甲科技", json.dumps(self.parties(case, lawyer), ensure_ascii=False))
        self.assertEqual(self.parties(case)["check"]["hits"][0]["case_id"], self.case_a)  # the partner sees it

    def test_a_hit_behind_an_information_wall_is_found_but_not_revealed(self):
        walled = self.new_case("隔离案件", [{"role": "opposing", "name": "己能源有限公司"}])["id"]
        wall = self.client.put(f"/api/case-walls/{walled}", headers=self.partner,
                               json={"reason": "关联案件对方当事人由本所代理", "insiders": ["另一位合伙人"]})
        self.assertEqual(wall.status_code, 200, wall.text)
        case = self.new_case("己能源委托", [{"role": "client", "name": "己能源有限公司"}])
        self.assertEqual(case["conflict"]["status"], "pending")  # the check still sees the walled case
        hit = case["conflict"]["check"]["hits"][0]
        self.assertTrue(hit["restricted"])
        self.assertNotIn("隔离案件", json.dumps(case, ensure_ascii=False))
        insider_view = self.parties(case["id"], self.partner2)["check"]["hits"][0]
        self.assertEqual((insider_view["restricted"], insider_view["case_id"]), (False, walled))


class LocalModeConflictTests(IsolatedDatabaseTestCase):
    def test_the_single_local_user_may_decide_but_the_audit_says_nobody_double_checked(self):
        from contextlib import closing

        from app.main import app
        from app.storage.db import connect, init_db

        init_db(seed=False)
        with TestClient(app) as client:
            client.post("/api/cases", json={"title": "原案", "parties": [{"role": "opposing", "name": "庚建设有限公司"}]})
            case = client.post("/api/cases", json={"title": "新案", "parties": [{"role": "client", "name": "庚建设有限公司"}]})
            case_id = case.json()["id"]
            self.assertEqual(case.json()["conflict"]["status"], "pending")
            decided = client.post(f"/api/cases/{case_id}/conflict-check/decision",
                                  json={"decision": "approve", "reason": "本机演示，已确认不存在实际冲突"})
            self.assertEqual(decided.status_code, 200, decided.text)
        with closing(connect()) as conn:
            detail = conn.execute("SELECT detail FROM audit_log WHERE case_id = ? AND action = '利益冲突放行'",
                                  (case_id,)).fetchone()[0]
        self.assertIn("未做双人复核", detail)
