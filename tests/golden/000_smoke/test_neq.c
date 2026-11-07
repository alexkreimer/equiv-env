/* Smoke test: CBMC refutes f(x) = x*2 vs g(x) = (x<<1) | 1 */
#include <assert.h>

int nondet_int(void);

int orig__f(int x) {
    return x * 2;
}

int cand__g(int x) {
    return (x << 1) | 1;
}

void harness(void) {
    int x = nondet_int();
    __CPROVER_assume(x >= 0 && x < 1073741824);  /* avoid UB on shift */
    int r_o = orig__f(x);
    int r_c = cand__g(x);
    assert(r_o == r_c);
}