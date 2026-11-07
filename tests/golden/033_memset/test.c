/* Golden test: memset equivalence against loop.
 *
 * Verifies that CBMC 6.11.0 correctly models memset.
 * Pairs: loop-based zero vs memset.
 *
 * Expected: proven.
 */

#include <assert.h>
#include <string.h>

#define MAXN 8

int nondet_int(void);
unsigned char nondet_uchar(void);

void orig_zero(unsigned char *arr, int n) {
    for (int i = 0; i < n; i++) arr[i] = 0;
}

void cand_zero(unsigned char *arr, int n) {
    memset(arr, 0, n);
}

void harness(void) {
    int n = nondet_int();
    __CPROVER_assume(n >= 0 && n <= MAXN);
    unsigned char a_o[MAXN], a_c[MAXN];
    for (int i = 0; i < MAXN; i++) {
        a_o[i] = nondet_uchar();
        a_c[i] = a_o[i];
    }
    orig_zero(a_o, n);
    cand_zero(a_c, n);
    for (int i = 0; i < MAXN; i++)
        assert(a_o[i] == a_c[i]);
}

/* Expected: proven on CBMC 6.11.0 --arch x86_64.
   Demonstrates CBMC models memset correctly. */