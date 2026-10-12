# avonhome-youtube-feed

에이본홈 홈페이지(https://magenta-meringue-978b8a.netlify.app)의 **CONTENTS** 영역에 보여 줄
유튜브 영상 목록을 자동으로 만드는 저장소입니다. (@avon_home · 채널 ID `UCfeNssjQJWU15LUk74vMkrA`)

- 공개 영상 정보와 자동화 코드만 있습니다. 비밀번호·API 키·개인정보는 넣지 않습니다.
- GitHub Free + 공개 저장소의 Actions(무료)만 씁니다. 유료 API·서비스는 쓰지 않습니다.

## 동작
1. **1시간마다** (매시 17분, UTC 기준) GitHub Actions가 `scripts/update_youtube.py`를 실행합니다.
2. YouTube RSS에서 최신 공개 영상 15개를 읽습니다.
3. 롱폼과 쇼츠를 구분합니다. RSS 링크(`/shorts/`)와 `youtube.com/shorts/{ID}` 응답(쇼츠=200, 롱폼=/watch로 이동)이 **둘 다 같을 때만** 분류하고, 다르면 넣지 않습니다.
4. **롱폼 최신 3개 + 쇼츠 최신 3개**를 고르고, 홈페이지용 제목에서 해시태그를 지웁니다.
5. 썸네일을 작은 webp로 만듭니다 (롱폼 640×360, 쇼츠 360×640 = 표지 가운데 세로 부분).
6. 내용이 바뀌었을 때만 `data/`를 commit합니다. 새 영상이 생겨도 Netlify는 다시 배포하지 않습니다.

## 파일
| 파일 | 내용 |
|---|---|
| `data/videos.json` | 홈페이지가 읽는 결과 (`long` 3개, `shorts` 3개) |
| `data/thumbs/*.webp` | 지금 표시 중인 6개 썸네일 |
| `data/status.json` | 마지막 확인·성공·오류 시각, 연속 실패 횟수 |
| `data/seed.json` | 첫 실행용 초기 데이터 (2026-10-12 검증한 실제 영상 6개) |
| `data/exclude.txt` | 홈페이지에서 숨길 영상 ID |
| `scripts/update_youtube.py` | 수집 스크립트 |
| `.github/workflows/update-youtube.yml` | 1시간 예약 실행 + 수동 실행 |

## 안전 장치
- **RSS 오류(404·500·빈 목록·잘못된 응답)** → `videos.json`을 **바꾸지 않습니다.** `status.json`에 오류만 기록하고, Actions 실행은 빨간색(실패)으로 표시됩니다.
- 결과가 비어 있으면 기존 데이터를 덮어쓰지 않습니다. 같은 영상은 한 번만 표시합니다.
- RSS 15개 밖으로 밀려난 영상은 oEmbed로 다시 확인합니다. **삭제·비공개(404/401/403)면 뺍니다.**
- 조회수 0인 예정 영상(프리미어·라이브 예약)은 조회수가 생길 때까지 넣지 않습니다.
- 모든 항목에 `origin`이 있습니다: `seed`(초기 데이터) / `rss`(자동 수집).

## 확인·복구
- **실행 기록:** 저장소 › Actions › *Update YouTube feed* (초록=정상, 빨강=RSS 오류로 데이터 유지)
- **즉시 갱신:** Actions › *Update YouTube feed* › **Run workflow**
- **RSS 장애 시험:** Run workflow에서 `simulate_rss_failure` 체크 → `videos.json`이 바뀌지 않아야 정상
- **예약 실행이 멈췄을 때:** GitHub은 공개 저장소에 60일 동안 활동이 없으면 예약을 멈춥니다. 하루 1회 `status.json` 기록으로 막고 있지만, 멈췄다면 Actions 화면에서 **Enable workflow**를 누릅니다.
- **영상 숨기기:** `data/exclude.txt`에 영상 ID를 추가합니다.

## 홈페이지에서 읽는 주소
`https://raw.githubusercontent.com/serenayoon/avonhome-youtube-feed/main/data/videos.json`
(CORS 허용. 반영까지 최대 약 5분 캐시)
