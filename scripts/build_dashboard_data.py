# 대시보드용 데이터 파일(data.json)을 만드는 스크립트
# - 하는 일: 깃허브 레포의 커밋(모든 브랜치) · 열린 이슈 · 열린 PR 을 읽어 파일 하나로 정리한다.
# - 누가 실행하나: 깃허브 Actions (.github/workflows/dashboard.yml). 내 PC 에서도 시험 삼아 실행할 수 있다.
# - 사용법: python scripts/build_dashboard_data.py _site/data.json
# - 읽기(GET)만 한다. 레포를 바꾸는 요청은 보내지 않는다.

import json  # JSON 읽고 쓰기
import os  # 환경변수 읽기, 폴더 만들기
import sys  # 명령줄에서 저장 위치 받기
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("TEAM_REPO") or "campteam3/project2"  # 읽을 레포
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT") or ""  # Actions 가 자동으로 넣어 주는 토큰
API = "https://api.github.com"
MAX_BRANCHES = 30  # 브랜치는 최대 30개까지만 본다
COMMITS_PER_BRANCH = 30  # 브랜치마다 최근 커밋 30개까지만 본다
MAX_ACTIVITY = 15  # "최근 활동" 에 보여 줄 개수


def github_get(path, params=None):
    """깃허브 API 에 읽기 요청을 보내고 결과를 돌려준다. 빈 레포(409)는 빈 목록으로 다룬다."""
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "team-dashboard-builder"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"  # 토큰은 보내기만 하고 출력하지 않는다
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 409:  # 커밋이 하나도 없는 빈 레포
            return []
        sys.exit(f"깃허브 요청 실패: {path} (HTTP {error.code})")  # 그 밖의 실패는 멈추고 이유를 알린다


def is_bot(user):
    """깃허브 봇 계정인지 확인한다. 봇은 팀원으로 세지 않는다."""
    user = user or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def read_commits(default_branch):
    """모든 브랜치의 최근 커밋을 모은다. 같은 커밋이 여러 브랜치에 있으면 한 번만 넣는다."""
    branches = [b["name"] for b in github_get(f"/repos/{REPO}/branches", {"per_page": 100})]
    # 기본 브랜치(main)를 맨 앞에 둔다. 그래야 main 에 있는 커밋은 main 것으로 표시된다
    branches.sort(key=lambda name: (name != default_branch, name))
    seen = {}  # 커밋 번호 → 커밋 정보
    for branch in branches[:MAX_BRANCHES]:
        items = github_get(f"/repos/{REPO}/commits", {"sha": branch, "per_page": COMMITS_PER_BRANCH})
        for item in items:
            if item["sha"] in seen or is_bot(item.get("author")):
                continue
            info = item["commit"]["author"]
            seen[item["sha"]] = {
                "sha": item["sha"][:7],
                "who": (item.get("author") or {}).get("login") or info.get("name") or "알 수 없음",
                "when": info["date"],  # 예: 2026-11-17T07:22:10Z (세계 표준시)
                "message": item["commit"]["message"].splitlines()[0][:120],  # 첫 줄만, 너무 길면 자른다
                "url": item["html_url"],
                "branch": branch,
            }
    return sorted(seen.values(), key=lambda c: c["when"], reverse=True)  # 최신순


def read_issues():
    """열린 이슈를 읽는다. 깃허브는 PR 도 섞어서 주기 때문에 PR 은 뺀다."""
    issues = []
    for item in github_get(f"/repos/{REPO}/issues", {"state": "open", "per_page": 100}):
        if "pull_request" in item:
            continue
        issues.append(
            {
                "number": item["number"],
                "title": item["title"][:160],
                "url": item["html_url"],
                "author": (item.get("user") or {}).get("login") or "알 수 없음",
                "assignees": [p["login"] for p in item.get("assignees", [])],
                "labels": [label["name"] for label in item.get("labels", [])],
                "created_at": item["created_at"],
            }
        )
    return issues


def read_pulls():
    """열린 PR 을 읽는다."""
    pulls = []
    for item in github_get(f"/repos/{REPO}/pulls", {"state": "open", "per_page": 100}):
        pulls.append(
            {
                "number": item["number"],
                "title": item["title"][:160],
                "url": item["html_url"],
                "author": (item.get("user") or {}).get("login") or "알 수 없음",
                "author_is_bot": is_bot(item.get("user")),
                "draft": bool(item.get("draft")),
                "reviewers": [p["login"] for p in item.get("requested_reviewers", [])],
                "head": item["head"]["ref"],
                "base": item["base"]["ref"],
                "created_at": item["created_at"],
            }
        )
    return pulls


def build_members(commits, issues, pulls):
    """팀원 목록을 만든다. 커밋했거나, 이슈를 배정받았거나, PR 을 올린 사람이 팀원이다."""
    members = {}

    def member(name):
        return members.setdefault(name.lower(), {"login": name, "last_commit": None, "open_issues": 0, "open_pulls": 0})

    for commit in commits:  # 최신순이라 사람마다 처음 만나는 커밋이 마지막 커밋이다
        person = member(commit["who"])
        if person["last_commit"] is None:
            person["last_commit"] = commit
    for issue in issues:
        for assignee in issue["assignees"]:
            member(assignee)["open_issues"] += 1
    for pull in pulls:
        if not pull["author_is_bot"]:
            member(pull["author"])["open_pulls"] += 1
    return sorted(members.values(), key=lambda m: m["login"].lower())


def build_activity(commits, issues, pulls):
    """커밋 · 새 이슈 · 새 PR 을 시간순으로 섞어 '최근 활동' 목록을 만든다."""
    activity = []
    for commit in commits:
        activity.append({"when": commit["when"], "who": commit["who"], "type": "commit", "text": commit["message"], "url": commit["url"]})
    for issue in issues:
        activity.append({"when": issue["created_at"], "who": issue["author"], "type": "issue", "text": f"#{issue['number']} {issue['title']}", "url": issue["url"]})
    for pull in pulls:
        activity.append({"when": pull["created_at"], "who": pull["author"], "type": "pull", "text": f"#{pull['number']} {pull['title']}", "url": pull["url"]})
    activity.sort(key=lambda a: a["when"], reverse=True)
    return activity[:MAX_ACTIVITY]


def main():
    output = sys.argv[1] if len(sys.argv) > 1 else "_site/data.json"  # 저장할 위치
    repo_info = github_get(f"/repos/{REPO}")
    default_branch = repo_info.get("default_branch") or "main"

    commits = read_commits(default_branch)
    issues = read_issues()
    pulls = read_pulls()

    data = {
        "repo": REPO,
        "repo_url": repo_info.get("html_url") or f"https://github.com/{REPO}",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # 이 파일을 만든 시각
        "members": build_members(commits, issues, pulls),
        "issues": issues,
        "pulls": pulls,
        "activity": build_activity(commits, issues, pulls),
    }

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=1)
    print(f"저장: {output} · 팀원 {len(data['members'])}명 · 이슈 {len(issues)}개 · PR {len(pulls)}개 · 커밋 {len(commits)}개")


if __name__ == "__main__":
    main()
