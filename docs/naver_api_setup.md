# 🔑 네이버 검색 API 발급 가이드

이 문서는 네이버 뉴스 크롤링에 필요한 **Client ID**와 **Client Secret**을 발급받는 방법을 단계별로 안내합니다.

> 📌 공식 참고 링크
> - [네이버 개발자 센터](https://developers.naver.com/main/)
> - [애플리케이션 등록 가이드](https://developers.naver.com/docs/common/openapiguide/appregister.md)
> - [뉴스 검색 API 레퍼런스](https://developers.naver.com/docs/serviceapi/search/news/news.md)

---

## 1단계. 네이버 계정으로 로그인

[네이버 개발자 센터](https://developers.naver.com/main/) 우측 상단 **로그인** 버튼을 클릭하여 네이버 계정으로 로그인합니다.

> 처음 이용하는 경우, 이용약관 동의 및 **휴대폰 번호 인증**이 필요합니다.

---

## 2단계. 애플리케이션 등록

1. 상단 메뉴에서 **Application → 애플리케이션 등록** 클릭
   - 직접 링크: [https://developers.naver.com/apps/#/register](https://developers.naver.com/apps/#/register)

2. **애플리케이션 이름** 입력 (예: `NewsGraph_Crawler`)

3. **사용 API** 드롭다운에서 **`검색`** 선택
   - 검색 API 하나만 선택하면 뉴스·블로그·이미지 등 모든 검색 엔드포인트 사용 가능.

4. **환경 추가** 섹션에서 **`WEB 서비스 URL`** 선택 후 `http://localhost` 입력
   - 로컬 개발 환경이므로 localhost로 설정합니다.

5. **등록하기** 버튼 클릭

---

## 3단계. Client ID / Client Secret 확인

1. 상단 메뉴 **Application → 내 애플리케이션** 클릭
   - 직접 링크: [https://developers.naver.com/apps/#/list](https://developers.naver.com/apps/#/list)

2. 방금 등록한 애플리케이션 선택

3. **개요** 탭에서 **Client ID**와 **Client Secret** 확인

---

## 4단계. `.env` 파일에 등록

프로젝트 루트의 `.env` 파일에 다음과 같이 입력합니다.

```env
NAVER_CLIENT_ID=발급받은_Client_ID
NAVER_CLIENT_SECRET=발급받은_Client_Secret
```

---

## 무료 할당량 (Rate Limit)

| 항목 | 무료 제공량 |
|------|------------|
| 일일 호출 한도 | 25,000 건/일 |
| 1회 최대 결과 수 | 100 건/요청 |

> 본 프로젝트는 `display=100`으로 한 번에 최대 100건을 가져오도록 설정되어 있습니다. 일반적인 사용 범위에서는 무료 할당량 초과 가능성이 매우 낮습니다.
