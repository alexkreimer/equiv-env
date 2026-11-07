/* Smoke test: CBMC proves f(x) = x*2 equivalent to g(x) = x+x */
#include <assert.h>

int nondet_int(void);

int orig__f(int x) {
    return x * 2;
}

int cand__g(int x) {
    return x + x;
}

void harness(void) {
    int x = nondet_int();
    __CPROVER_assume(x > -1073741824 && x < 1073741824);  /* avoid signed overflow */
    int r_o = orig__f(x);
    int r_c = cand__g(x);
    assert(r_o == r_c);
}