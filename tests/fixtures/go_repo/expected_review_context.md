## Review Target
- Repo root: <FIXTURE_REPO>
- Go module: example.com/fixture
- Focus file count: 1
- Focus files:
  - internal/auth/jwt.go
- Focus package dirs:
  - internal/auth

## Focus Package: internal/auth
- Package name: auth
- Import path: example.com/fixture/internal/auth
- Package doc: Package auth validates JWTs for incoming requests.
- Focused files in this package:
  - jwt.go
- Package files:
  - jwt.go
  - jwt_test.go
- Exported symbols:
  - ValidateToken
  - ParseClaims
  - TokenClaims
  - ErrInvalidToken
  - DefaultIssuer
- Test symbols:
  - TestValidateToken_RejectsEmpty
  - BenchmarkValidateToken
- Notable imports:
  - fmt
  - strings
  - example.com/fixture/internal/config
  - testing
- Internal deps:
  - example.com/fixture/internal/config

## Focused Repo Map
### Focused Repo Map
- `internal/auth/jwt.go`
  - package: auth
  - exported symbols: ValidateToken, ParseClaims, TokenClaims, ErrInvalidToken, DefaultIssuer
