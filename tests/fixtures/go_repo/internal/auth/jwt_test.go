package auth

import "testing"

func TestValidateToken_RejectsEmpty(t *testing.T) {
	if _, err := ValidateToken(""); err == nil {
		t.Fatal("expected error")
	}
}

func BenchmarkValidateToken(b *testing.B) {
	for i := 0; i < b.N; i++ {
		_, _ = ValidateToken("token")
	}
}
