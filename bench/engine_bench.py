"""ADR-002(벡터화 룰 평가) / ADR-003(bitset MWIS) 성능 가설 검증 프로토타입."""
import time, random, numpy as np

random.seed(7); rng = np.random.default_rng(7)
N = 3000  # 전국 확장 시나리오 최대치

# ---- 컴파일된 스냅샷 모사: 정책을 "필드별 열 배열"로 뒤집어 둔다 -------------
SENT = np.iinfo(np.int16).min          # 해당 조건 없음(무제약) 표시
age_min   = rng.integers(18, 30, N).astype(np.int16)
age_max   = rng.integers(30, 40, N).astype(np.int16)
res_min   = np.where(rng.random(N) < .6, rng.integers(0, 13, N), SENT).astype(np.int16)
inc_max   = np.where(rng.random(N) < .7, rng.integers(80, 201, N), SENT).astype(np.int16)
emp_mask  = rng.integers(1, 32, N).astype(np.int8)     # 취업상태 5종 비트마스크
region    = rng.integers(0, 300, N).astype(np.int32)   # 0 = 전국
# unknown 유발 필드(유사사업 이력 등): 있으면 사용자 답변 필요
needs_ans = rng.random(N) < .25

def judge(age, res_m, inc, emp_bit, user_regions, answered):
    ok = (age_min <= age) & (age <= age_max)
    m = res_min != SENT;  ok &= ~m | (res_min <= res_m)
    m = inc_max != SENT;  ok &= ~m | (inc <= inc_max)
    ok &= (emp_mask & emp_bit) != 0
    ok &= np.isin(region, user_regions)
    unknown = needs_ans & ~answered & ok          # 다른 조건은 통과했는데 정보만 부족
    eligible = ok & ~unknown
    return eligible, unknown, ~ok

user_regions = np.array([0, 41, 165], dtype=np.int32)
answered = np.zeros(N, bool)

judge(25, 4, 120, 2, user_regions, answered)       # 워밍업
t = time.perf_counter()
for _ in range(1000):
    e, u, bad = judge(25, 4, 120, 2, user_regions, answered)
rule_ms = (time.perf_counter() - t) / 1000 * 1000
print(f"[ADR-002] 룰 평가 {N}개 정책 x 5필드 : {rule_ms:.3f} ms/요청  "
      f"(적격 {e.sum()} / 확인필요 {u.sum()} / 부적격 {bad.sum()})")

# ---- ADR-003: bitset 분기한정 MWIS (PuLP/CBC 없이 정확해) --------------------
def mwis(weights, adj):
    best = [0, 0]
    def rec(cand, cur_w, cur_set):
        if cur_w + sum(weights[i] for i in bits(cand)) <= best[0]:
            return                                   # 상계 가지치기
        if not cand:
            if cur_w > best[0]: best[0], best[1] = cur_w, cur_set
            return
        v = (cand & -cand).bit_length() - 1
        rec(cand & ~(1 << v) & ~adj[v], cur_w + weights[v], cur_set | (1 << v))  # v 포함
        rec(cand & ~(1 << v), cur_w, cur_set)                                    # v 제외
    rec((1 << len(weights)) - 1, 0, 0)
    return best

def bits(m):
    while m:
        b = m & -m; yield b.bit_length() - 1; m ^= b

def brute(weights, adj):
    n, best = len(weights), (0, 0)
    for s in range(1 << n):
        if any((s >> i) & 1 and (adj[i] & s) for i in range(n)): continue
        w = sum(weights[i] for i in range(n) if (s >> i) & 1)
        if w > best[0]: best = (w, s)
    return list(best)

# G4 게이트: 정점 <=12 케이스 20건을 완전탐색과 대조 (기준 100% 일치)
agree = 0
for _ in range(20):
    n = random.randint(6, 12)
    w = [random.randint(1, 30) * 100000 for _ in range(n)]
    adj = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            if random.random() < .35:
                adj[i] |= 1 << j; adj[j] |= 1 << i
    agree += (mwis(w, adj)[0] == brute(w, adj)[0])
print(f"[ADR-003] 솔버 정확성 (정점<=12, 20건 완전탐색 대조) : {agree}/20 일치")

# 실사용 규모: 적격 정책 30개
n = 30
w = [random.randint(1, 30) * 100000 for _ in range(n)]
adj = [0] * n
for i in range(n):
    for j in range(i + 1, n):
        if random.random() < .25:
            adj[i] |= 1 << j; adj[j] |= 1 << i
t = time.perf_counter()
for _ in range(100): mwis(w, adj)
print(f"[ADR-003] MWIS 정점 30개 정확해 : {(time.perf_counter()-t)/100*1000:.3f} ms/요청")

# 최악 가정: 적격 64개 (실사용상 발생하지 않는 수준)
n = 64
w = [random.randint(1, 30) * 100000 for _ in range(n)]
adj = [0] * n
for i in range(n):
    for j in range(i + 1, n):
        if random.random() < .30:
            adj[i] |= 1 << j; adj[j] |= 1 << i
t = time.perf_counter(); mwis(w, adj)
print(f"[ADR-003] MWIS 정점 64개(최악) 정확해 : {(time.perf_counter()-t)*1000:.1f} ms")

# ---- 스냅샷 메모리 실측 ------------------------------------------------------
cols = [age_min, age_max, res_min, inc_max, emp_mask, region, needs_ans]
print(f"[ADR-001] 룰 열배열 메모리({N}개 정책, 7필드) : {sum(c.nbytes for c in cols)/1024:.1f} KB")
