/* Golden test: overflow under -O3.
 *
 * A candidate that relies on signed overflow wrap-around (which is UB in C)
 * must be rejected by CBMC with --signed-overflow-check enabled.
 *
 * f(x) = x                 (identity)
 * g(x) = x + 1 - 1         (identity under wrap-around, UB in C)
 *
 * Under two's-complement wrap-around: x+1-1 = x for ALL x (including INT_MAX).
 *   - INT_MAX + 1 wraps to INT_MIN, then INT_MIN - 1 wraps back to INT_MAX.
 * Under C semantics: x+1 at INT_MAX is UB → compiler may do anything at -O3.
 *
 * Expected with --signed-overflow-check (default ON):
 *   cand__g has UB → not_equivalent.
 *
 * Expected with --no-signed-overflow-check:
 *   CBMC uses wrap-around semantics → x+1-1 == x → proven equivalent.
 *   But this verdict is UNSOUND on real hardware with -O3.
 *
 * This proves why --no-signed-overflow-check must NEVER be used
 * for candidate checks.
 */

#include <assert.h>

int nondet_int(void);

int orig__f(int x) {
    return x;
}

int cand__g(int x) {
    return x + 1 - 1;
}

void harness(void) {
    int x = nondet_int();
    int r_o = orig__f(x);
    int r_c = cand__g(x);
    assert(r_o == r_c);
}