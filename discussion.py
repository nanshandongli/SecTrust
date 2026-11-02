"""
discussion.py —— 同事圆桌（任务讨论室）。

目的：让多位 AI 同事围绕「同一个任务」顺序交换看法、相互回应，形成共识后再去干活——
与 Ballot（并行交卷择优）、Arbiter（事实冲突裁决）互补：讨论是「决策前的 deliberation」，
不是比赛，也不是仲裁。

数据模型：
  rooms(room_id PK, title, objective, participants JSON, task_link, status, created_at, updated_at)
  turns(turn_id PK, room_id, round, speaker_id, speaker_name, role_label, profile_id,
        content, available, model, created_at, meta JSON)
  syntheses(synth_id PK, room_id, version, moderator_id, moderator_name, content,
            recommended_approach, available, created_at)

纪律（与 threads.py / console_chat.py 对齐）：
- 存储：SQLite WAL + thread-local 连接 + 缺输入即报错 + 删库自动重建；
- 诚实边界：某同事的算力 profile 未配置 → 该轮如实标记「缺席/不可用」，
  绝不拿别的模型假装它发言（no-fake）；综合环节 LLM 不可用同样如实回报；
- 参与者解析：岗位(designer/writer/...)经 role_routing 取绑定算力，未绑定诚实回退 active；
  内置专家(exp-*)作为独立视角，由 active 算力代讲并明确标注「代讲」；
- 讨论是内部、无出域、低风险；发言若注入了内容防护则做轻量脱敏（防 PII 复述）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

DEFAULT_DB = os.path.join("data", "discussion.db")

ROLES_KNOWN = ("designer", "writer", "auditor", "analyst", "publisher", "admin")


class DiscussionError(ValueError):
    """讨论室业务错误（带可直接展示给用户的中文原因）。"""


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


class DiscussionStore:
    """讨论室存储。db_path 可注入；连接管理遵循项目纪律。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.environ.get("SECTRUST_DISCUSSION_DB", DEFAULT_DB)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._local = threading.local()
        self._closed = False
        with self._conn() as conn:
            self._ensure_schema(conn)

    # ---- 连接：thread-local + 显式 close ----
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA busy_timeout=30000;")
            self._local.conn = c
        return c

    def _ensure_schema(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rooms ("
            " room_id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL DEFAULT '',"
            " objective TEXT NOT NULL,"
            " participants TEXT NOT NULL DEFAULT '[]',"
            " task_link TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'open',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS turns ("
            " turn_id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " round INTEGER NOT NULL,"
            " speaker_id TEXT NOT NULL,"
            " speaker_name TEXT NOT NULL,"
            " role_label TEXT NOT NULL DEFAULT '',"
            " profile_id TEXT NOT NULL DEFAULT '',"
            " content TEXT NOT NULL,"
            " available INTEGER NOT NULL DEFAULT 1,"
            " model TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL,"
            " meta TEXT NOT NULL DEFAULT '{}')")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_room"
            " ON turns(room_id, created_at)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS syntheses ("
            " synth_id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " version INTEGER NOT NULL,"
            " moderator_id TEXT NOT NULL DEFAULT '',"
            " moderator_name TEXT NOT NULL DEFAULT '',"
            " content TEXT NOT NULL,"
            " recommended_approach TEXT NOT NULL DEFAULT '',"
            " available INTEGER NOT NULL DEFAULT 1,"
            " created_at REAL NOT NULL)")
        # 旧库迁移：task_link 列后加
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)").fetchall()}
        if "task_link" not in cols:
            conn.execute("ALTER TABLE rooms ADD COLUMN task_link TEXT NOT NULL DEFAULT ''")
        conn.commit()

    def close(self):
        if self._closed:
            return
        self._closed = True
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            finally:
                self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 房间 ----
    def create_room(self, title, objective, participants, task_id=None,
                    room_id=None) -> dict:
        if not objective or not objective.strip():
            raise DiscussionError("讨论目标不能为空")
        if not participants:
            raise DiscussionError("至少需要一位参与同事")
        now = time.time()
        rid = room_id or _new_id("room")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rooms"
                " (room_id, title, objective, participants, task_link, status,"
                "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (rid, title or objective[:40], objective.strip(),
                 json.dumps(participants, ensure_ascii=False),
                 task_id or "", "open", now, now))
        return self.get_room(rid)

    def get_room(self, room_id) -> dict:
        if not room_id:
            raise DiscussionError("room_id 不能为空")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT room_id, title, objective, participants, task_link, "
                "status, created_at, updated_at FROM rooms WHERE room_id=?",
                (room_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_room(row)

    @staticmethod
    def _row_to_room(r) -> dict:
        return {"room_id": r[0], "title": r[1], "objective": r[2],
                "participants": json.loads(r[3]) if r[3] else [],
                "task_link": r[4], "status": r[5],
                "created_at": r[6], "updated_at": r[7]}

    def list_rooms(self, limit=50) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT room_id, title, objective, status, task_link, "
                "created_at, updated_at FROM rooms ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"room_id": r[0], "title": r[1], "objective": r[2], "status": r[3],
                 "task_link": r[4], "created_at": r[5], "updated_at": r[6]}
                for r in rows]

    def set_status(self, room_id, status) -> bool:
        with self._conn() as conn:
            conn.execute("UPDATE rooms SET status=?, updated_at=? WHERE room_id=?",
                         (status, time.time(), room_id))
            return conn.total_changes >= 0

    def set_task_link(self, room_id, task_link) -> bool:
        with self._conn() as conn:
            conn.execute("UPDATE rooms SET task_link=?, updated_at=? WHERE room_id=?",
                         (task_link or "", time.time(), room_id))
            return True

    # ---- 发言 ----
    def add_turn(self, room_id, round_no, speaker, content, available,
                 model="", reply_to=None) -> dict:
        if self.get_room(room_id) is None:
            raise DiscussionError("讨论室不存在: %s" % room_id)
        msg = {"turn_id": _new_id("turn"), "room_id": room_id, "round": round_no,
               "speaker_id": speaker.get("colleague_id", ""),
               "speaker_name": speaker.get("name", ""),
               "role_label": speaker.get("role_label", ""),
               "profile_id": speaker.get("profile_id", "") or "",
               "content": content, "available": 1 if available else 0,
               "model": model or "",
               "created_at": time.time(),
               "meta": {"reply_to": reply_to or []}}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO turns(turn_id, room_id, round, speaker_id, "
                " speaker_name, role_label, profile_id, content, available, "
                " model, created_at, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg["turn_id"], room_id, round_no, msg["speaker_id"],
                 msg["speaker_name"], msg["role_label"], msg["profile_id"],
                 content, msg["available"], msg["model"], msg["created_at"],
                 json.dumps(msg["meta"], ensure_ascii=False)))
            conn.execute("UPDATE rooms SET updated_at=? WHERE room_id=?",
                         (msg["created_at"], room_id))
        return msg

    def turns(self, room_id, limit=1000) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT turn_id, room_id, round, speaker_id, speaker_name, "
                "role_label, profile_id, content, available, model, created_at, meta"
                " FROM turns WHERE room_id=? ORDER BY created_at ASC LIMIT ?",
                (room_id, limit)).fetchall()
        return [self._row_to_turn(r) for r in rows]

    @staticmethod
    def _row_to_turn(r) -> dict:
        return {"turn_id": r[0], "room_id": r[1], "round": r[2], "speaker_id": r[3],
                "speaker_name": r[4], "role_label": r[5], "profile_id": r[6],
                "content": r[7], "available": bool(r[8]), "model": r[9],
                "created_at": r[10], "meta": json.loads(r[11] or "{}")}

    # ---- 综合 ----
    def save_synthesis(self, room_id, moderator, content, recommended,
                       available) -> dict:
        version = len(self.syntheses(room_id)) + 1
        synth = {"synth_id": _new_id("synth"), "room_id": room_id,
                 "version": version,
                 "moderator_id": moderator.get("colleague_id", ""),
                 "moderator_name": moderator.get("name", ""),
                 "content": content, "recommended_approach": recommended or "",
                 "available": 1 if available else 0, "created_at": time.time()}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO syntheses(synth_id, room_id, version, moderator_id, "
                " moderator_name, content, recommended_approach, available, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (synth["synth_id"], room_id, version, synth["moderator_id"],
                 synth["moderator_name"], content, recommended or "",
                 synth["available"], synth["created_at"]))
        return synth

    def syntheses(self, room_id) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT synth_id, room_id, version, moderator_id, moderator_name, "
                "content, recommended_approach, available, created_at"
                " FROM syntheses WHERE room_id=? ORDER BY version DESC",
                (room_id,)).fetchall()
        return [{"synth_id": r[0], "room_id": r[1], "version": r[2],
                 "moderator_id": r[3], "moderator_name": r[4], "content": r[5],
                 "recommended_approach": r[6], "available": bool(r[7]),
                 "created_at": r[8]} for r in rows]

    def latest_synthesis(self, room_id) -> dict:
        rs = self.syntheses(room_id)
        return rs[0] if rs else None


class DiscussionService:
    """讨论编排：参与者解析 + 发言轮 + 综合 + 落任务。

    依赖注入：profiles(ModelProfileStore) / llm(LlmEngine) / plan_service(可选) /
    content_guard(可选)。llm 必须有 speak() 方法（见 console_chat.LlmEngine）。
    """

    def __init__(self, profiles, llm, plan_service=None, content_guard=None,
                 store=None):
        self._profiles = profiles
        self._llm = llm
        self._plan = plan_service
        self._guard = content_guard
        self._store = store or DiscussionStore()

    # ------------------------------------------------------------------
    # 参与者解析
    # ------------------------------------------------------------------
    def resolve_participants(self, specs) -> list:
        """把角色/专家 id 列表解析成参与者清单（去重）。"""
        from console_api import ROLES, BUILTIN_EXPERTS
        from role_routing import binding_for

        out, seen = [], set()
        for spec in (specs or []):
            sid = str(spec or "").strip()
            if not sid:
                continue
            if sid.startswith("exp-"):
                expert = next((e for e in BUILTIN_EXPERTS if e["id"] == sid), None)
                if expert is None:
                    continue
                p = {"colleague_id": expert["id"], "name": expert["name"],
                     "role_label": expert.get("kind", "内置专家"),
                     "role_brief": expert.get("desc", ""), "profile_id": "",
                     "is_expert": True}
            else:
                role = next((r for r in ROLES if r["id"] == sid), None)
                if role is None:
                    continue
                p = {"colleague_id": "role:" + role["id"], "name": role["name"],
                     "role_label": role["name"], "role_brief": role.get("desc", ""),
                     "profile_id": binding_for(sid), "is_expert": False}
            if p["colleague_id"] in seen:
                continue
            seen.add(p["colleague_id"])
            out.append(p)
        return out

    def catalog(self) -> dict:
        """可供前端勾选的同事清单（角色 + 内置专家 + 当前绑定算力）。"""
        from console_api import ROLES, BUILTIN_EXPERTS
        from role_routing import binding_for, colleague_label
        roles = [{"id": r["id"], "name": r["name"], "desc": r.get("desc", ""),
                  "kind": "岗位", "bound_profile": binding_for(r["id"]),
                  "bound_label": colleague_label(self._profiles, binding_for(r["id"]))}
                 for r in ROLES]
        experts = [{"id": e["id"], "name": e["name"], "desc": e.get("desc", ""),
                    "kind": e.get("kind", "内置专家"), "bound_profile": "",
                    "bound_label": "由当前算力代讲"} for e in BUILTIN_EXPERTS]
        return {"roles": roles, "experts": experts,
                "active_id": self._profiles.get_active_id()}

    # ------------------------------------------------------------------
    # 建室
    # ------------------------------------------------------------------
    def create_room(self, title, objective, specs, task_id=None) -> dict:
        participants = self.resolve_participants(specs)
        room = self._store.create_room(title, objective, participants,
                                      task_id=task_id)
        room["participants"] = participants
        return room

    # ------------------------------------------------------------------
    # 发言轮
    # ------------------------------------------------------------------
    def run_round(self, room_id, rounds=1, max_round_total=6) -> dict:
        room = self._store.get_room(room_id)
        if room is None:
            raise DiscussionError("讨论室不存在: %s" % room_id)
        participants = room["participants"]
        existing = self._store.turns(room_id, limit=1000)
        current_round = (max(t["round"] for t in existing) + 1) if existing else 1

        produced = []
        for _ in range(max(1, rounds)):
            if current_round > max_round_total:
                break
            prior = self._store.turns(room_id, limit=1000)
            reply_to = [t["turn_id"] for t in prior]
            for p in participants:
                turn = self._speak_one(room, p, current_round, prior)
                saved = self._store.add_turn(
                    room_id, current_round, p, turn["content"], turn["available"],
                    model=turn.get("model", ""), reply_to=reply_to)
                saved["available"] = turn["available"]
                produced.append(saved)
            current_round += 1
        return {"room_id": room_id, "round": current_round - 1,
                "turns": produced, "status": room["status"]}

    def _resolve_engine(self, requested_pid):
        """解析实际出声的算力：指定且已配置优先；否则诚实回退 active。"""
        if requested_pid:
            prof = self._profiles.get(requested_pid)
            if prof is not None and prof.configured:
                return prof, requested_pid
        active = self._profiles.get_active()
        if active is not None and active.configured:
            return active, self._profiles.get_active_id()
        return None, None

    def _speak_one(self, room, p, round_no, prior) -> dict:
        engine, actual_pid = self._resolve_engine(p["profile_id"])
        base = {"speaker_id": p["colleague_id"], "speaker_name": p["name"],
                "role_label": p["role_label"], "round": round_no,
                "profile_id": p["profile_id"] or (actual_pid or "")}
        if engine is None:
            # 诚实缺席：不调模型、不替身
            base.update({
                "available": False,
                "content": ("（该同事的算力模型未配置，本轮缺席；可在「设置」里为"
                            "其绑定/配置模型后继续讨论）"),
                "model": ""})
            return base
        system_prompt = self._persona_system(p)
        user_prompt = self._round_user(room["objective"], prior, round_no)
        user_prompt = self._redact(user_prompt)
        try:
            out = self._llm.speak(system_prompt, user_prompt,
                                  model=actual_pid, timeout=90)
            content = (out.get("answer") or "").strip()
            if not content:
                raise RuntimeError("empty answer")
        except Exception as e:  # noqa: BLE001
            base.update({"available": False,
                         "content": "（调用算力失败：%s）" % str(e),
                         "model": out.get("model") if isinstance(out, dict) else ""})
            return base
        base.update({"available": True, "content": content,
                     "model": out.get("model", "")})
        return base

    @staticmethod
    def _persona_system(p) -> str:
        base = (
            "你是「%s」这位 AI 同事，岗位视角：%s。\n"
            "你正与另外几位 AI 同事围绕同一个任务进行「圆桌讨论」——目的是在动手前"
            "把各自的专业看法说清楚、相互回应，最终形成更好的任务方案。\n"
            "请基于任务目标和前面同事的观点，用你的专业视角真诚地发表见解：可以赞同、"
            "补充，也可以提出不同意见或风险。不要客套，不要简单重复别人说过的话。"
        ) % (p["name"], p["role_brief"])
        if p.get("is_expert"):
            base += ("\n（你是以「内置专家」身份参与，由平台当前算力代讲，"
                     "请始终保持专家视角。）")
        return base

    @staticmethod
    def _round_user(objective, prior, round_no) -> str:
        lines = ["任务目标：\n" + objective]
        if prior:
            lines.append("\n前面同事的观点（按发生顺序）：")
            for t in prior:
                tag = t["speaker_name"] + ("（缺席）" if not t["available"] else "")
                snippet = (t["content"] or "").replace("\n", " ")[:600]
                lines.append("- [%s · 第%d轮] %s" % (tag, t["round"], snippet))
            prompt = ("\n请回应上面其他同事的观点，给出你的补充、赞同或不同意见"
                      "（第%d轮）。" % round_no)
        else:
            prompt = ("\n这是讨论的开场轮，还没有人发言。请作为你的岗位视角，"
                      "就上面的任务目标发表你的开场看法。")
        lines.append(prompt)
        return "\n".join(lines)

    def _redact(self, text: str) -> str:
        if self._guard is None:
            return text
        try:
            return self._guard.redact(text)
        except Exception:  # noqa: BLE001
            return text

    # ------------------------------------------------------------------
    # 综合共识
    # ------------------------------------------------------------------
    def synthesize(self, room_id, moderator_spec=None) -> dict:
        room = self._store.get_room(room_id)
        if room is None:
            raise DiscussionError("讨论室不存在: %s" % room_id)
        turns = self._store.turns(room_id, limit=1000)
        if not turns:
            raise DiscussionError("还没有任何发言，无法综合")
        moderator = self._pick_moderator(room, moderator_spec)
        engine, actual_pid = self._resolve_engine(moderator["profile_id"])
        if engine is None:
            return {"room_id": room_id, "available": False, "synth_id": None,
                    "moderator": moderator["name"],
                    "content": ("（综合所需的算力模型不可用，无法生成综合；"
                                "发言已保留，可稍后重试）"),
                    "recommended_approach": ""}
        transcript = self._format_transcript(turns)
        system_prompt = (
            "你是「%s」，负责为这场圆桌讨论做综合主持人。请通读各同事的观点，"
            "产出一份综合纪要，严格包含以下四节：\n"
            "【共识点】各方基本一致的结论；\n"
            "【分歧】仍不一致的地方；\n"
            "【未决问题】还需要人拍板或补充的点；\n"
            "【建议落地方案】给「任务中心」去执行的具体建议（一句话需求级，"
            "要让拆解引擎能直接照着建图）。"
        ) % moderator["name"]
        user_prompt = ("任务目标：\n" + room["objective"]
                      + "\n\n讨论记录：\n" + transcript)
        user_prompt = self._redact(user_prompt)
        try:
            out = self._llm.speak(system_prompt, user_prompt,
                                  model=actual_pid, timeout=120)
            content = (out.get("answer") or "").strip()
            if not content:
                raise RuntimeError("empty answer")
        except Exception as e:  # noqa: BLE001
            return {"room_id": room_id, "available": False, "synth_id": None,
                    "moderator": moderator["name"],
                    "content": "（综合调用失败：%s）" % str(e),
                    "recommended_approach": ""}
        recommended = self._extract_recommendation(content)
        synth = self._store.save_synthesis(
            room_id, moderator, content, recommended, available=True)
        self._store.set_status(room_id, "synthesized")
        return {"room_id": room_id, "available": True, "synth_id": synth["synth_id"],
                "moderator": moderator["name"], "content": content,
                "recommended_approach": recommended,
                "version": synth["version"]}

    @staticmethod
    def _pick_moderator(room, spec) -> dict:
        participants = room["participants"]
        if spec:
            m = next((p for p in participants if p["colleague_id"] == spec), None)
            if m:
                return m
        writer = next((p for p in participants
                      if p["colleague_id"] == "role:writer"), None)
        if writer:
            return writer
        return participants[0]

    @staticmethod
    def _format_transcript(turns) -> str:
        out = []
        for t in turns:
            tag = t["speaker_name"] + ("（缺席）" if not t["available"] else "")
            out.append("[第%d轮 · %s]\n%s" % (t["round"], tag, t["content"] or ""))
        return "\n\n".join(out)

    @staticmethod
    def _extract_recommendation(content: str) -> str:
        """从综合纪要里抽取「建议落地方案」一节（最后一段优先）。

        兼容两种写法：方括号标记【建议落地方案】与 Markdown 标题
        （如「## 四、建议落地方案」）——模型不一定用方括号。
        """
        if not content:
            return ""
        for marker in ("【建议落地方案】", "建议落地方案", "【建议】", "建议落地方案：",
                      "建议落地方案:", "建议："):
            idx = content.find(marker)
            if idx >= 0:
                tail = content[idx + len(marker):].strip()
                # 截到下一个【节】或下一个 Markdown 标题之前（如果有）
                for stop in ("【", "\n##", "\n#"):
                    nxt = tail.find(stop)
                    if nxt > 0:
                        tail = tail[:nxt].strip()
                        break
                return tail
        return ""

    # ------------------------------------------------------------------
    # 落到任务
    # ------------------------------------------------------------------
    def to_task(self, room_id, operator="operator") -> dict:
        room = self._store.get_room(room_id)
        if room is None:
            raise DiscussionError("讨论室不存在: %s" % room_id)
        synth = self._store.latest_synthesis(room_id)
        if synth is None or not synth["available"]:
            raise DiscussionError("请先完成「综合共识」再落到任务")
        requirement = self._build_requirement(room, synth)
        plan_svc = self._plan or self._lazy_plan()
        try:
            view = plan_svc.build(requirement, operator=operator)
        except Exception as e:  # noqa: BLE001
            raise DiscussionError("落任务失败：%s" % e)
        self._store.set_task_link(room_id, view.get("plan_id") or "")
        view["room_id"] = room_id
        view["discussion_synthesis"] = synth.get("content", "")
        return view

    @staticmethod
    def _build_requirement(room, synth) -> str:
        rec = (synth.get("recommended_approach") or "").strip()
        if rec:
            return "%s\n\n（圆桌讨论后的落地建议：%s）" % (room["objective"], rec)
        return room["objective"]

    def _lazy_plan(self):
        from plan_service import PlanService
        if self._plan is None:
            self._plan = PlanService()
        return self._plan

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def room_view(self, room_id) -> dict:
        room = self._store.get_room(room_id)
        if room is None:
            raise DiscussionError("讨论室不存在: %s" % room_id)
        return {"room": room, "turns": self._store.turns(room_id, limit=1000),
                "synthesis": self._store.latest_synthesis(room_id)}
