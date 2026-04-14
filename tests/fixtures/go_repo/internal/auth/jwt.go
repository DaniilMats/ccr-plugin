// Package auth validates JWTs for incoming requests.
package auth

import (
	"fmt"
	"strings"

	"example.com/fixture/internal/config"
)

// TokenClaims holds normalized claims extracted from a token.
type TokenClaims struct {
	Subject string
}

var ErrInvalidToken = fmt.Errorf("invalid token")

const DefaultIssuer = "fixture"

func ValidateToken(raw string) (*TokenClaims, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return nil, ErrInvalidToken
	}
	if config.DefaultAudience == "" {
		return nil, fmt.Errorf("missing audience")
	}
	return &TokenClaims{Subject: trimmed}, nil
}

func ParseClaims(raw string) (*TokenClaims, error) {
	return ValidateToken(raw)
}

func localHelper(raw string) string {
	return strings.TrimSpace(raw)
}
