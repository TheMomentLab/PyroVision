# PyroVision 문서

PyroVision 프로젝트의 기술 문서 모음입니다.

## 📚 문서 목록

### 1. [화재 융합 로드맵](FIRE_FUSION_ROADMAP.md)
**EO-IR 센서 융합 시스템 개발 계획**

Phase별로 구성된 센서 융합 기능 개발 로드맵입니다:
- **Phase 1**: IR 게이트키퍼 (완료 ✅)
- **Phase 2**: 좌표 캘리브레이션
- **Phase 3**: 시간 동기화
- **Phase 4-5**: 신뢰도 계산 + 상태 머신
- **Phase 6**: 다중 화점 추적
- **Phase 7**: 적응형 임계값

**읽어야 할 사람**: 센서 융합 알고리즘 개발자, 시스템 아키텍트

---

### 2. [PyQt GUI 설계서](pyqt_gui_design.md)
**PyQt6 기반 GUI 인터페이스 설계**

CLI 기반 시스템을 PyQt6 GUI로 확장하기 위한 설계 문서입니다:
- 메인 창 레이아웃 (상태바, 영상 패널, 제어 패널)
- 아키텍처 및 주요 클래스
- 5단계 구현 계획
- PyQt6 구성 요소 및 의존성

**읽어야 할 사람**: GUI 개발자, UX/UI 담당자

---

### 3. [리팩토링 로드맵](REFACTORING_ROADMAP.md)
**코드 품질 개선 계획**

현재 코드베이스(5.6/10)를 8.0/10 수준으로 개선하기 위한 3단계 리팩토링 계획입니다:
- **Phase 1** (8시간): 긴급 수정 - 예외 처리, 로깅, 리소스 정리
- **Phase 2** (12시간): 구조 개선 - 모듈화, Config 검증, 타입 힌트
- **Phase 3** (7시간): 고도화 - 설정 추상화, 문서화, 단위 테스트

**읽어야 할 사람**: 모든 개발자, 코드 리뷰어, 프로젝트 관리자

---

## 🗺️ 추천 읽기 순서

### 신규 개발자
1. 프로젝트 [README](../README.md) - 전체 개요 파악
2. [리팩토링 로드맵](REFACTORING_ROADMAP.md) - 현재 코드 상태 및 개선 방향 이해
3. [화재 융합 로드맵](FIRE_FUSION_ROADMAP.md) - 핵심 알고리즘 이해

### GUI 개발자
1. [PyQt GUI 설계서](pyqt_gui_design.md)
2. [리팩토링 로드맵](REFACTORING_ROADMAP.md) Phase 2.1 (GUI 아키텍처)

### 알고리즘 개발자
1. [화재 융합 로드맵](FIRE_FUSION_ROADMAP.md)
2. [리팩토링 로드맵](REFACTORING_ROADMAP.md) Phase 1.2, 1.3 (IR/RGB 안정성)

---

## 📝 문서 작성 가이드

새로운 문서를 추가할 때는 다음을 준수해주세요:

1. **파일명**: `UPPERCASE_WITH_UNDERSCORES.md` 형식
2. **헤더**: 프로젝트명(PyroVision) 포함
3. **구조**: 목차, 개요, 상세 내용, 변경 이력 순서
4. **이 README 업데이트**: 새 문서 추가 시 목록에 포함

---

## 🔗 관련 링크

- **메인 README**: [../README.md](../README.md)
- **설정 파일**: [../configs/](../configs/)
- **테스트**: [../tests/](../tests/)
- **GitHub**: [Stellar-Moment/PyroVision](https://github.com/Stellar-Moment/PyroVision)
