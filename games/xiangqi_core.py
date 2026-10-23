"""
中国象棋规则引擎（零依赖，纯标准库）。

只做"规则"这一层：棋盘表示、7 类棋子走法、合法性过滤、将军 / 将死 / 困毙判定。
不含任何 I/O、不含进程协调——那部分交给 xiangqi_table.py（复用 task_registry 的底座）。

坐标约定：
- 10 行（0..9）x 9 列（0..8）。
- 红方在下方：红将初始 row=0，红兵初始 row=3，红方半场 row 0..4，红"前进"= row 增大。
- 黑方在上方：黑将初始 row=9，黑兵初始 row=6，黑方半场 row 5..9，黑"前进"= row 减小。
- 河界在 row 4 与 row 5 之间。红兵过河后（row>=5）可横走；黑兵过河后（row<=4）可横走。
- 九宫：红 row 0..2 / 黑 row 7..9，均 col 3..5。

棋盘用 10x9 的二维列表：`board[r][c]` 为 None 或 `(color, ptype)`。
color ∈ {'r','b'}；ptype ∈ {'G','A','E','H','R','C','P'}（将/士/象/马/车/炮/兵）。
"""

from __future__ import annotations

ROWS, COLS = 10, 9

# 棋子基础价值（用于最简评估）
VALUE = {'G': 10000, 'R': 900, 'C': 450, 'H': 400, 'E': 200, 'A': 200, 'P': 100}

# 红黑前进方向（row 增量）
FORWARD = {'r': 1, 'b': -1}


def opponent(color: str) -> str:
    return 'b' if color == 'r' else 'r'


def in_palace(color: str, r: int = 0, c: int = 0) -> bool:
    if not (3 <= c <= 5):
        return False
    return (0 <= r <= 2) if color == 'r' else (7 <= r <= 9)


def in_board(r: int, c: int) -> bool:
    return 0 <= r < ROWS and 0 <= c < COLS


def own_side(color: str, r: int) -> bool:
    """象不能过河：红方半场 row 0..4，黑方半场 row 5..9。"""
    return (r <= 4) if color == 'r' else (r >= 5)


def initial_board() -> list[list]:
    """标准开局。返回 10x9 二维列表。"""
    empty = [[None] * COLS for _ in range(ROWS)]
    # 后排（红 row0 / 黑 row9）：车马象士将士兵
    back = ['R', 'H', 'E', 'A', 'G', 'A', 'E', 'H', 'R']
    for c, t in enumerate(back):
        empty[0][c] = ('r', t)
        empty[9][c] = ('b', t)
    # 炮 row1 / row7
    empty[2][1] = ('r', 'C')
    empty[2][7] = ('r', 'C')
    empty[7][1] = ('b', 'C')
    empty[7][7] = ('b', 'C')
    # 兵 row3 / row6
    for c in (0, 2, 4, 6, 8):
        empty[3][c] = ('r', 'P')
        empty[6][c] = ('b', 'P')
    return empty


def clone(board: list[list]) -> list[list]:
    return [row[:] for row in board]


def find_general(board: list[list], color: str):
    for r in range(ROWS):
        for c in range(COLS):
            p = board[r][c]
            if p and p[0] == color and p[1] == 'G':
                return r, c
    return None


# --------------------------------------------------------------------------
# 伪合法走法（不考虑"走完是否被将军"）
# --------------------------------------------------------------------------
def pseudo_moves(board: list[list], r: int, c: int) -> list[tuple[int, int]]:
    p = board[r][c]
    if not p:
        return []
    color, t = p
    moves = []
    fwd = FORWARD[color]

    def add(tr, tc):
        if not in_board(tr, tc):
            return
        tp = board[tr][tc]
        if tp is None or tp[0] != color:  # 空格或敌子可走/可吃
            moves.append((tr, tc))

    if t == 'G':  # 将：九宫内一步直行 + 对脸飞将（把敌将当作可"吃"的目标）
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tr, tc = r + dr, c + dc
            if in_palace(color, tr, tc):
                add(tr, tc)
        # 飞将：同一列、中间无子，则可"吃"掉对面将（= 一步制胜）
        for dr in (1, -1):
            tr = r + dr
            while in_board(tr, c):
                tp = board[tr][c]
                if tp is not None:
                    if tp[0] != color and tp[1] == 'G':
                        moves.append((tr, c))
                    break
                tr += dr

    elif t == 'A':  # 士：九宫内一步斜行
        for dr, dc in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            tr, tc = r + dr, c + dc
            if in_palace(color, tr, tc):
                add(tr, tc)

    elif t == 'E':  # 象：田字格、塞象眼、不过河
        for dr, dc in ((2, 2), (2, -2), (-2, 2), (-2, -2)):
            tr, tc = r + dr, c + dc
            if not in_board(tr, tc) or not own_side(color, tr):
                continue
            er, ec = r + dr // 2, c + dc // 2  # 象眼
            if board[er][ec] is None:
                add(tr, tc)

    elif t == 'H':  # 马：日字、蹩马腿
        # (腿方向, 落点方向)
        legs = [
            ((0, 1), (1, 2)), ((0, 1), (-1, 2)),
            ((0, -1), (1, -2)), ((0, -1), (-1, -2)),
            ((1, 0), (2, 1)), ((1, 0), (2, -1)),
            ((-1, 0), (-2, 1)), ((-1, 0), (-2, -1)),
        ]
        for (lr, lc), (mr, mc) in legs:
            if not in_board(r + lr, c + lc) or board[r + lr][c + lc] is not None:
                continue  # 马腿越界或被挡
            add(r + mr, c + mc)

    elif t == 'R':  # 车：直线滑行
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tr, tc = r + dr, c + dc
            while in_board(tr, tc):
                tp = board[tr][tc]
                if tp is None:
                    moves.append((tr, tc))
                else:
                    if tp[0] != color:
                        moves.append((tr, tc))
                    break
                tr += dr
                tc += dc

    elif t == 'C':  # 炮：平移如车；吃子需隔一子（炮架）
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tr, tc = r + dr, c + dc
            # 平移段
            while in_board(tr, tc) and board[tr][tc] is None:
                moves.append((tr, tc))
                tr += dr
                tc += dc
            # 越过第一个子（炮架）后找第一个敌子
            if in_board(tr, tc):  # 炮架存在
                tr += dr
                tc += dc
                while in_board(tr, tc):
                    tp = board[tr][tc]
                    if tp is not None:
                        if tp[0] != color:
                            moves.append((tr, tc))
                        break
                    tr += dr
                    tc += dc

    elif t == 'P':  # 兵：向前；过河后可横走；永不后退
        add(r + fwd, c)
        crossed = (r >= 5) if color == 'r' else (r <= 4)
        if crossed:
            add(r, c + 1)
            add(r, c - 1)

    return moves


# --------------------------------------------------------------------------
# 被将军判定（用"从将出发的反向射线扫描"，比枚举所有敌子走法快得多）
# --------------------------------------------------------------------------
def in_check(board: list[list], color: str) -> bool:
    g = find_general(board, color)
    if not g:
        return True  # 将已被吃，视为已被将死
    gr, gc = g
    opp = opponent(color)

    # 1) 直线：车 / 炮 / 飞将
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        tr, tc = gr + dr, gc + dc
        screen = False
        while in_board(tr, tc):
            tp = board[tr][tc]
            if tp is not None:
                if not screen:
                    # 第一个子：车或将对脸
                    if tp[0] == opp and (tp[1] == 'R' or tp[1] == 'G'):
                        return True
                    screen = True
                else:
                    # 第二个子：炮（恰好隔一个炮架）
                    if tp[0] == opp and tp[1] == 'C':
                        return True
                    break
            tr += dr
            tc += dc

    # 2) 马：检查 8 个可能"跳过来将军"的马的落点及对应马腿
    horse_attacks = [
        ((gr - 2, gc - 1), (gr - 1, gc)), ((gr - 2, gc + 1), (gr - 1, gc)),
        ((gr + 2, gc - 1), (gr + 1, gc)), ((gr + 2, gc + 1), (gr + 1, gc)),
        ((gr - 1, gc - 2), (gr, gc - 1)), ((gr + 1, gc - 2), (gr, gc - 1)),
        ((gr - 1, gc + 2), (gr, gc + 1)), ((gr + 1, gc + 2), (gr, gc + 1)),
    ]
    for (hr, hc), (lr, lc) in horse_attacks:
        if in_board(hr, hc) and in_board(lr, lc):
            p = board[hr][hc]
            if p and p[0] == opp and p[1] == 'H' and board[lr][lc] is None:
                return True

    # 3) 兵：敌兵能走到将的位置
    #    红兵前进=+1(row)，过河后可横走；黑兵前进=-1(row)。
    #    所以"将"被红兵攻击 <=> 红兵在 (gr-1,gc) 或 (过河红兵在 gr,gc±1)。
    fwd_opp = FORWARD[opp]
    # 正前方（敌兵向前一步踩到将）
    pr, pc = gr - fwd_opp, gc
    if in_board(pr, pc):
        p = board[pr][pc]
        if p and p[0] == opp and p[1] == 'P':
            return True
    # 两侧（敌兵已过河且横走一步踩到将）
    crossed = (pr >= 5) if opp == 'r' else (pr <= 4)
    if crossed:
        for dc in (-1, 1):
            pc2 = gc + dc
            if in_board(gr, pc2):
                p = board[gr][pc2]
                if p and p[0] == opp and p[1] == 'P':
                    return True

    return False


def make_move(board: list[list], fr: int, fc: int, tr: int, tc: int) -> list[list]:
    nb = clone(board)
    nb[tr][tc] = nb[fr][fc]
    nb[fr][fc] = None
    return nb


def legal_moves(board: list[list], color: str) -> list[tuple]:
    out = []
    for r in range(ROWS):
        for c in range(COLS):
            p = board[r][c]
            if not p or p[0] != color:
                continue
            for (tr, tc) in pseudo_moves(board, r, c):
                nb = make_move(board, r, c, tr, tc)
                if not in_check(nb, color):
                    out.append((r, c, tr, tc))
    return out


def is_game_over(board: list[list], color_to_move: str) -> tuple[bool, str | None]:
    """轮到 color_to_move 时是否终局。
    无合法步 => 终局，且轮到谁谁输（涵盖将死与被困毙/困毙）。
    返回 (over, winner)。winner 是胜方颜色。"""
    if any(True for _ in legal_moves(board, color_to_move)):
        return False, None
    return True, opponent(color_to_move)


# --------------------------------------------------------------------------
# 极简搜索：negamax + alpha-beta + 时间预算。用于让"AI 同事"会下、会杀。
# --------------------------------------------------------------------------
import time as _time

_MATE = 1_000_000

# 和棋规则：三次重复局面（含轮次方）+ 步数上限。与 xiangqi_table 共用同一常量，
# 保证"引擎以为的和棋"与"棋局表判的和棋"是同一个标准。
MAX_PLIES = 300
REPETITION_LIMIT = 3


def position_key(board: list[list], color_to_move: str) -> str:
    """局面的规范化指纹：棋盘 + 轮次方。用于重复局面检测与搜索内避循。"""
    cell = {('r', 'G'): 'K', ('r', 'A'): 'A', ('r', 'E'): 'B', ('r', 'H'): 'N',
            ('r', 'R'): 'R', ('r', 'C'): 'C', ('r', 'P'): 'P',
            ('b', 'G'): 'k', ('b', 'A'): 'a', ('b', 'E'): 'b', ('b', 'H'): 'n',
            ('b', 'R'): 'r', ('b', 'C'): 'c', ('b', 'P'): 'p'}
    rows = []
    for row in board:
        rows.append("".join(cell.get(p, ".") for p in row))
    return "/".join(rows) + "|" + color_to_move


def evaluate(board: list[list], color: str) -> float:
    """子力 + 位置项。位置项驱动"求进步"（否则弱引擎会无限打转）。"""
    score = 0.0
    for r in range(ROWS):
        for c in range(COLS):
            p = board[r][c]
            if not p:
                continue
            pcol, t = p
            v = VALUE[t]
            sign = 1 if pcol == color else -1
            pos = 0.0
            if t == 'P':
                crossed = (r >= 5) if pcol == 'r' else (r <= 4)
                if crossed:
                    pos += 30  # 过河兵价值上升
                    # 逼近对方底线（红 row 7/8、黑 row 1/2）再加成
                    if (r >= 7) if pcol == 'r' else (r <= 2):
                        pos += 30
            elif t in ('R', 'C', 'H'):
                pos += max(0, 4 - abs(c - 4)) * 2  # 中路略优
            score += sign * (v + pos)
    if in_check(board, opponent(color)):
        score += 30  # 叫将小奖励
    return score


def _order_moves(board: list[list], moves: list) -> list:
    """吃子优先（按被吃价值降序），加速 alpha-beta 剪枝。"""
    return sorted(moves, key=lambda m: 0 if board[m[2]][m[3]] is None
                  else VALUE.get(board[m[2]][m[3]][1], 0), reverse=True)


_QMAX = 8  # 静态搜索最大深度（防止长吃子链爆炸）


def _quiescence(board: list[list], color: str, alpha: float, beta: float,
                deadline: float, path: frozenset[str],
                qdepth: int = 0) -> float:
    """静态搜索：只搜吃子（被将时搜全部应将），搜到"无吃子可走"为止。
    消除水平线效应——否则浅层搜索会把"白吃一个子"错判成优势，
    看不到对手随后的反吃（开局双方马都被炮隔子瞄住，正是此类的典型）。"""
    moves = legal_moves(board, color)
    if not moves:
        return -_MATE  # 轮到 color 却无步可走 => color 输
    key = position_key(board, color)
    if key in path:
        return 0.0  # 重复局面按和棋计
    if _time.time() > deadline or qdepth >= _QMAX:
        return evaluate(board, color)
    chk = in_check(board, color)
    stand_pat = evaluate(board, color)
    if not chk:
        if stand_pat >= beta:
            return stand_pat
        if stand_pat > alpha:
            alpha = stand_pat
        cand = [m for m in moves if board[m[2]][m[3]] is not None]  # 只搜吃子
    else:
        cand = moves  # 被将：不能站着不动，必须搜全部应将
    best = stand_pat if not chk else -float('inf')
    for (fr, fc, tr, tc) in _order_moves(board, cand):
        nb = make_move(board, fr, fc, tr, tc)
        val = -_quiescence(nb, opponent(color), -beta, -alpha, deadline,
                           path | {key}, qdepth + 1)
        if val > best:
            best = val
        if val > alpha:
            alpha = val
        if alpha >= beta:
            return best
    return best


def _negamax(board: list[list], color: str, depth: int, alpha: float,
             beta: float, deadline: float,
             path: frozenset[str] = frozenset()) -> float:
    moves = legal_moves(board, color)
    if not moves:
        # 轮到 color 却无步可走 => color 输。越早将死分越高。
        return -_MATE
    key = position_key(board, color)
    if key in path:
        return 0.0  # 重复局面：按和棋计，迫使引擎寻求进步而非打转
    if depth == 0 or _time.time() > deadline:
        return _quiescence(board, color, alpha, beta, deadline, path)
    best = -float('inf')
    for (fr, fc, tr, tc) in _order_moves(board, moves):
        nb = make_move(board, fr, fc, tr, tc)
        val = -_negamax(nb, opponent(color), depth - 1, -beta, -alpha, deadline,
                        path | {key})
        if val > best:
            best = val
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def choose_move(board: list[list], color: str, depth: int = 3,
                time_budget: float = 2.0) -> tuple | None:
    """为 color 选一步。迭代加深 + 时间预算：任一层超时即保留上一层完整结果。
    优先直接绝杀（mate in 1）；无合法步返回 None。"""
    moves = legal_moves(board, color)
    if not moves:
        return None
    deadline = _time.time() + time_budget
    # 先看有没有一步绝杀，有就直接走，省搜索
    for (fr, fc, tr, tc) in moves:
        nb = make_move(board, fr, fc, tr, tc)
        over, winner = is_game_over(nb, opponent(color))
        if over and winner == color:
            return (fr, fc, tr, tc)
    root_key = position_key(board, color)
    best_move = moves[0]
    for d in range(1, depth + 1):
        completed = True
        local_best, local_val = None, -float('inf')
        for m in moves:
            if _time.time() > deadline:
                completed = False  # 超时：放弃本层，沿用上一层完整深度
                break
            nb = make_move(board, *m)
            val = -_negamax(nb, opponent(color), d - 1, -float('inf'),
                            float('inf'), deadline, frozenset({root_key}))
            if val > local_val:
                local_val, local_best = val, m
        if not completed or local_best is None:
            break
        best_move = local_best
    return best_move


# --------------------------------------------------------------------------
# 序列化（便于在共享 SQLite 里存棋盘）
# --------------------------------------------------------------------------
def board_to_json(board: list[list]) -> list:
    return [[(list(p) if p else None) for p in row] for row in board]


def board_from_json(data: list) -> list[list]:
    return [[(tuple(cell) if cell else None) for cell in row] for row in data]


def move_to_str(m: tuple) -> str:
    fr, fc, tr, tc = m
    col_ch = "abcdefghi"
    return f"{col_ch[fc]}{fr + 1}{col_ch[tc]}{tr + 1}"


if __name__ == "__main__":
    b = initial_board()
    print("开局红方合法步数:", len(legal_moves(b, 'r')))
    print("红是否被将:", in_check(b, 'r'), "| 黑是否被将:", in_check(b, 'b'))
    mv = choose_move(b, 'r', depth=3)
    print("红方第一步:", move_to_str(mv) if mv else None)
