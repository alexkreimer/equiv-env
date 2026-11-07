/* Golden test: wrong memset length must be caught.
 *
 * Original zeros n bytes, candidate zeros n-1 bytes (wrong).
 *
 * Expected: not_equivalent.
 */

#include <assert.h>
#include <string.h>

#define MAXN 4

int nondet_int(void);
unsigned char nondet_uchar(void);

void orig_func(unsigned char *arr, int n) {
    memset(arr, 0, n);
}

void cand_func(unsigned char *arr, int n) {
    memset(arr, 0, n - 1);  /* wrong: misses last byte */
}

void harness(void) {
    int n = nondet_int();
    __CPROVER_assume(n >= 1 && n <= MAXN);
    unsigned char a_o[MAXN], a_c[MAXN];
    for (int i = 0; i < MAXN; i++) {
        a_o[i] = nondet_uchar();
        a_c[i] = a_o[i];
    }
    orig_func(a_o, n);
    cand_func(a_c, n);
    for (int i = 0; i < MAXN; i++)
        assert(a_o[i] == a_c[i]);
}

/* Expected: not_equivalent.
   When n > 0, the candidate fails to zero the last byte.
   This validates that the harness catches wrong memset usage
   (and that CBMC doesn't silently accept all memsets). */