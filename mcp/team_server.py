# 우리 조 MCP 서버 (읽기 전용)
# - 하는 일: 깃허브 조 레포의 커밋 · 이슈 · PR 을 읽어서 팀 현황을 알려 준다.
# - 쓰기 · 지우기 도구는 일부러 만들지 않았다. (이슈 만들기 등은 깃허브 공식 MCP 가 맡는다)
# - 실행: python mcp/team_server.py  (Claude Code 가 .mcp.json 을 보고 대신 실행해 준다)
# - 필요한 것: pip install mcp  (2.x 버전 기준)

import json  # 깃허브가 보내 주는 JSON 글자를 파이썬 자료로 바꾼다
import os  # 환경변수(TEAM_REPO, GITHUB_PAT)를 읽는다
import urllib.error  # 깃허브 요청이 실패했을 때의 에러 종류
import urllib.parse  # 주소 뒤에 붙는 조건(?state=open 등)을 만든다
import urllib.request  # 깃허브에 요청을 보낸다 (파이썬 기본 기능이라 따로 설치할 것이 없다)
from datetime import datetime, timedelta, timezone  # 시간 계산

# mcp 2.x 에서는 FastMCP 가 MCPServer 로 이름이 바뀌었다 (1.x: from mcp.server.fastmcp import FastMCP)
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations  # 도구에 "읽기 전용" 꼬리표를 붙일 때 쓴다

# ───────────── 설정 ─────────────
REPO = os.environ.get("TEAM_REPO", "campteam3/project2")  # 읽을 레포. 환경변수 TEAM_REPO 로 바꿀 수 있다
TOKEN = os.environ.get("GITHUB_PAT", "")  # 토큰. 공개 레포는 없어도 되고, 비공개 레포는 꼭 필요하다
WORKING_HOURS = 1  # 최근 이 시간(1시간) 안에 커밋이 있으면 "작업 중" 으로 본다
KST = timezone(timedelta(hours=9))  # 한국 시간 (깃허브는 세계 표준시로 알려 준다)
API = "https://api.github.com"  # 깃허브 공식 API 주소

# 서버 만들기. instructions 는 AI 에게 이 서버가 무엇인지 알려 주는 한 줄 소개다
mcp = MCPServer(
    "team",
    instructions=f"우리 조 깃허브 레포({REPO})의 팀 현황 · 최근 커밋 · 열린 이슈 · 열린 PR 을 읽기 전용으로 알려 준다.",
)

# 세 도구 모두 읽기만 한다는 꼬리표 (AI 와 사용자에게 "이 도구는 아무것도 바꾸지 않는다" 고 알린다)
READ_ONLY = ToolAnnotations(readOnlyHint=True)


# ───────────── 깃허브에서 읽어 오기 (도구들이 같이 쓰는 부분) ─────────────
def _github_get(path: str, params: dict | None = None):
    """깃허브 API 에 GET(읽기) 요청을 보낸다. (자료, 에러 문장) 을 돌려준다."""
    url = f"{API}{path}"  # 예: https://api.github.com/repos/조직/레포/commits
    if params:
        url += "?" + urllib.parse.urlencode(params)  # 조건을 주소 뒤에 붙인다
    headers = {
        "Accept": "application/vnd.github+json",  # 깃허브가 권하는 응답 형식
        "User-Agent": "team-mcp-server",  # 깃허브는 이 값이 없으면 요청을 거절한다
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"  # 토큰이 있으면 같이 보낸다 (출력은 절대 하지 않는다)
    request = urllib.request.Request(url, headers=headers, method="GET")  # GET 만 쓴다 = 읽기만 한다
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # 15초 안에 답이 없으면 포기
            return json.loads(response.read().decode("utf-8")), None  # 성공: 자료와 "에러 없음"
    except urllib.error.HTTPError as error:
        if error.code == 409:  # 409 = 레포가 비어 있음 (커밋이 하나도 없음)
            return [], None  # 에러가 아니라 "빈 목록" 으로 다룬다
        if error.code == 404:  # 404 = 레포가 없거나, 비공개인데 토큰 권한이 없음
            return None, f"레포 {REPO} 를 찾을 수 없어요. 이름이 맞는지, 비공개라면 GITHUB_PAT 권한이 있는지 확인하세요."
        if error.code == 401:  # 401 = 토큰이 틀렸거나 만료됨
            return None, "깃허브 토큰(GITHUB_PAT)이 틀렸거나 만료됐어요."
        if error.code == 403:  # 403 = 권한이 없거나, 요청을 너무 많이 보냄
            return None, "깃허브가 요청을 거절했어요. 토큰 없이 너무 많이 물었거나(1시간에 60번 제한) 권한이 없어요."
        return None, f"깃허브 요청이 실패했어요. (HTTP {error.code})"
    except Exception as error:  # 인터넷이 끊긴 경우 등
        return None, f"깃허브에 연결하지 못했어요. ({type(error).__name__})"


def _read_commits():
    """최근 커밋을 최대 100개 읽는다. 사람 이름 · 시각 · 메시지만 골라 단순한 목록으로 만든다."""
    data, error = _github_get(f"/repos/{REPO}/commits", {"per_page": 100})
    if error:
        return None, error
    commits = []
    for item in data:
        author = item.get("author") or {}  # 깃허브 계정 정보 (계정과 연결 안 된 커밋이면 비어 있다)
        info = item["commit"]["author"]  # 커밋에 적힌 이름과 시각
        commits.append(
            {
                "who": author.get("login") or info.get("name") or "알 수 없음",  # 깃허브 아이디, 없으면 커밋에 적힌 이름
                "when": datetime.fromisoformat(info["date"].replace("Z", "+00:00")),  # 글자를 시간 자료로 바꾼다
                "message": item["commit"]["message"].splitlines()[0],  # 커밋 메시지의 첫 줄만
                "sha": item["sha"][:7],  # 커밋 번호 앞 7자리
            }
        )
    return commits, None  # 깃허브가 최신순으로 주기 때문에 이 목록도 최신순이다


def _read_open_issues():
    """열린 이슈를 최대 100개 읽는다. PR 은 뺀다 (깃허브는 PR 도 이슈 목록에 섞어서 준다)."""
    data, error = _github_get(f"/repos/{REPO}/issues", {"state": "open", "per_page": 100})
    if error:
        return None, error
    issues = []
    for item in data:
        if "pull_request" in item:  # 이 칸이 있으면 이슈가 아니라 PR 이다
            continue
        issues.append(
            {
                "number": item["number"],  # 이슈 번호
                "title": item["title"],  # 제목
                "assignees": [person["login"] for person in item.get("assignees", [])],  # 담당자 아이디들
                "labels": [label["name"] for label in item.get("labels", [])],  # 라벨 이름들
            }
        )
    return issues, None


def _read_open_pulls():
    """열린 PR(풀 리퀘스트)을 최대 100개 읽는다. 번호 · 제목 · 올린 사람 · 리뷰어 · 브랜치만 고른다."""
    data, error = _github_get(f"/repos/{REPO}/pulls", {"state": "open", "per_page": 100})
    if error:
        return None, error
    pulls = []
    for item in data:
        pulls.append(
            {
                "number": item["number"],  # PR 번호
                "title": item["title"],  # 제목
                "author": (item.get("user") or {}).get("login") or "알 수 없음",  # PR 을 올린 사람
                "draft": bool(item.get("draft")),  # 아직 작성 중(초안)인지
                "reviewers": [person["login"] for person in item.get("requested_reviewers", [])],  # 리뷰를 부탁받은 사람들
                "branch": item["head"]["ref"],  # 작업한 브랜치 이름
                "base": item["base"]["ref"],  # 합쳐질 브랜치 이름 (보통 main)
                "when": datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),  # PR 을 올린 시각
            }
        )
    return pulls, None


def _time_text(when: datetime) -> str:
    """시간 자료를 '11/17 15:30 (2시간 전)' 같은 읽기 쉬운 글자로 바꾼다."""
    minutes = int((datetime.now(timezone.utc) - when).total_seconds() // 60)  # 지금으로부터 몇 분 전인지
    if minutes < 60:
        ago = f"{max(minutes, 0)}분 전"
    elif minutes < 60 * 24:
        ago = f"{minutes // 60}시간 전"
    else:
        ago = f"{minutes // (60 * 24)}일 전"
    return f"{when.astimezone(KST).strftime('%m/%d %H:%M')} ({ago})"  # 한국 시간으로 보여 준다


def _same(a: str, b: str) -> bool:
    """이름을 비교할 때 대소문자와 앞뒤 빈칸은 무시한다."""
    return a.strip().lower() == b.strip().lower()


# ───────────── 도구 4개(AI 는 아래 설명글을 읽고 어떤 도구를 쓸지 고른다) ─────────────
@mcp.tool(title="팀 현황", annotations=READ_ONLY)
def team_status() -> str:
    """팀원 전체의 현재 상태를 한 번에 보여 준다.

    팀원마다 (1) 지금 작업 중인지, (2) 마지막 커밋 시각과 메시지, (3) 맡고 있는 열린 이슈 수,
    (4) 올려 둔 열린 PR 수를 알려 준다.
    '작업 중' 은 최근 1시간 안에 커밋이 있다는 뜻이다.
    팀원 목록은 따로 등록하지 않고, 레포에 커밋했거나 이슈를 배정받았거나 PR 을 올린 사람을 자동으로 모은다.
    "누가 작업 중이야?", "팀 진행 상황 알려 줘", "요즘 조용한 사람 있어?" 같은 질문에 쓴다.
    입력값은 없다. 읽기 전용이다.
    """
    commits, error = _read_commits()
    if error:
        return error
    issues, error = _read_open_issues()
    if error:
        return error
    pulls, error = _read_open_pulls()
    if error:
        return error

    members = {}  # 팀원 이름(소문자) → 그 사람의 정보

    def member(name: str) -> dict:
        """처음 보는 팀원이면 빈 칸을 만들어 주고, 그 사람의 정보를 돌려준다."""
        return members.setdefault(name.lower(), {"name": name, "last": None, "issues": 0, "pulls": 0})

    for commit in commits:  # 최신순이라, 사람마다 처음 만나는 커밋이 그 사람의 마지막 커밋이다
        person = member(commit["who"])
        if person["last"] is None:
            person["last"] = commit
    for issue in issues:  # 이슈 담당자마다 열린 이슈 수를 1씩 올린다
        for assignee in issue["assignees"]:
            member(assignee)["issues"] += 1
    for pull in pulls:  # PR 을 올린 사람마다 열린 PR 수를 1씩 올린다
        member(pull["author"])["pulls"] += 1

    if not members:  # 커밋도 이슈도 PR 도 없는 새 레포
        return f"{REPO} 에는 아직 커밋도, 배정된 이슈도, 열린 PR 도 없어요. 팀원이 활동을 시작하면 여기에 나타나요."

    limit = datetime.now(timezone.utc) - timedelta(hours=WORKING_HOURS)  # 이 시각 이후 커밋이면 "작업 중"
    lines = [f"[{REPO}] 팀 현황 · 팀원 {len(members)}명 (작업 중 = 최근 {WORKING_HOURS}시간 안에 커밋)"]
    for person in sorted(members.values(), key=lambda p: p["name"].lower()):  # 이름순으로 정렬
        last = person["last"]
        counts = f"열린 이슈 {person['issues']}개 · 열린 PR {person['pulls']}개"  # 모든 줄 끝에 붙는 숫자들
        if last is None:  # 이슈나 PR 만 있고 최근 커밋 100개 안에 없는 사람
            lines.append(f"- {person['name']}: 커밋 없음 · {counts}")
            continue
        state = "작업 중" if last["when"] >= limit else "쉬는 중"
        lines.append(
            f"- {person['name']}: {state} · 마지막 커밋 {_time_text(last['when'])} "
            f"\"{last['message']}\" · {counts}"
        )
    return "\n".join(lines)  # 여러 줄을 하나의 글로 합쳐서 돌려준다


@mcp.tool(title="최근 커밋", annotations=READ_ONLY)
def recent_commits(member: str = "", count: int = 5) -> str:
    """팀원이 최근에 한 작업(커밋)을 최신순으로 보여 준다.

    커밋마다 누가 · 언제 · 어떤 메시지로 올렸는지 알려 준다.
    "○○ 마지막으로 뭐 했어?", "최근에 올라온 작업 보여 줘" 같은 질문에 쓴다.

    member: 깃허브 아이디 (예: "hong-gildong"). 비우면 팀 전체의 커밋을 보여 준다.
    count: 보여 줄 개수. 1~30, 기본 5.
    레포의 최근 커밋 100개 안에서 찾는다. 읽기 전용이다.
    """
    commits, error = _read_commits()
    if error:
        return error
    if not commits:
        return f"{REPO} 에는 아직 커밋이 없어요."

    if member.strip():  # 팀원을 지정했으면 그 사람 것만 남긴다
        commits = [commit for commit in commits if _same(commit["who"], member)]
        if not commits:  # 없는 사람을 넣어도 죽지 않고 빈 결과를 알려 준다
            return f"'{member}' 의 커밋을 찾지 못했어요. 깃허브 아이디가 맞는지 확인하세요."

    count = max(1, min(count, 30))  # 개수를 1~30 사이로 맞춘다
    target = member.strip() or "팀 전체"
    lines = [f"[{REPO}] {target} 최근 커밋 {min(count, len(commits))}개"]
    for commit in commits[:count]:  # 앞에서부터 count 개만
        lines.append(f"- {_time_text(commit['when'])} · {commit['who']} · {commit['message']} ({commit['sha']})")
    return "\n".join(lines)


@mcp.tool(title="열린 이슈", annotations=READ_ONLY)
def open_issues(assignee: str = "") -> str:
    """아직 닫히지 않은(열린) 이슈 목록을 보여 준다.

    이슈마다 번호 · 제목 · 담당자 · 라벨을 알려 준다. PR 은 포함하지 않는다.
    "남은 이슈 뭐 있어?", "○○ 가 맡은 이슈 보여 줘", "담당자 없는 이슈 있어?" 같은 질문에 쓴다.

    assignee: 담당자 깃허브 아이디. 비우면 열린 이슈 전체를 보여 준다.
    최대 100개까지 읽는다. 읽기 전용이라 이슈를 만들거나 닫지 못한다.
    """
    issues, error = _read_open_issues()
    if error:
        return error
    if not issues:
        return f"{REPO} 에는 열린 이슈가 없어요."

    if assignee.strip():  # 담당자를 지정했으면 그 사람 것만 남긴다
        issues = [issue for issue in issues if any(_same(name, assignee) for name in issue["assignees"])]
        if not issues:
            return f"'{assignee}' 에게 배정된 열린 이슈가 없어요."

    target = assignee.strip() or "전체"
    lines = [f"[{REPO}] 열린 이슈 {len(issues)}개 ({target})"]
    for issue in issues:
        who = ", ".join(issue["assignees"]) or "담당자 없음"
        labels = ", ".join(issue["labels"]) or "라벨 없음"
        lines.append(f"- #{issue['number']} {issue['title']} · 담당: {who} · 라벨: {labels}")
    return "\n".join(lines)


@mcp.tool(title="열린 PR", annotations=READ_ONLY)
def open_pull_requests(author: str = "") -> str:
    """아직 머지되거나 닫히지 않은(열린) PR(풀 리퀘스트) 목록을 보여 준다.

    PR 마다 번호 · 제목 · 올린 사람 · 올린 시각 · 브랜치 · 리뷰를 부탁받은 사람 · 초안 여부를 알려 준다.
    "리뷰 기다리는 PR 있어?", "○○ 가 올린 PR 보여 줘", "머지 안 된 작업 뭐 있어?" 같은 질문에 쓴다.

    author: PR 을 올린 사람의 깃허브 아이디. 비우면 열린 PR 전체를 보여 준다.
    최대 100개까지 읽는다. 읽기 전용이라 PR 을 만들거나 머지하거나 닫지 못한다.
    """
    pulls, error = _read_open_pulls()
    if error:
        return error
    if not pulls:
        return f"{REPO} 에는 열린 PR 이 없어요."

    if author.strip():  # 올린 사람을 지정했으면 그 사람 것만 남긴다
        pulls = [pull for pull in pulls if _same(pull["author"], author)]
        if not pulls:
            return f"'{author}' 가 올린 열린 PR 이 없어요."

    target = author.strip() or "전체"
    lines = [f"[{REPO}] 열린 PR {len(pulls)}개 ({target})"]
    for pull in pulls:
        reviewers = ", ".join(pull["reviewers"]) or "리뷰어 없음"
        draft = " [초안]" if pull["draft"] else ""  # 초안이면 제목 뒤에 표시한다
        lines.append(
            f"- #{pull['number']} {pull['title']}{draft} · 올린 사람: {pull['author']} · {_time_text(pull['when'])} "
            f"· {pull['branch']} → {pull['base']} · 리뷰어: {reviewers}"
        )
    return "\n".join(lines)


# 이 파일을 직접 실행했을 때만 서버를 켠다 (stdio = Claude Code 가 이 프로그램과 글자를 주고받는 방식)
if __name__ == "__main__":
    mcp.run()
