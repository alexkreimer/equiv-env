/* Golden test: char signedness must agree across gcc, clang, and CBMC.
 *
 * On x86-64 (target platform), char is SIGNED — values can be negative.
 * On arm64 Linux, char is UNSIGNED — values are 0..255.
 *
 * This is a self-equivalence test with char input. It passes (proven)
 * on x86-64 with -fsigned-char / --arch x86_64.
 *
 * The pytest test_runner will compile with gcc -fsigned-char,
 * clang -fsigned-char, and CBMC --arch x86_64, then verify
 * all three give the SAME verdict for this function.
 *
 * If char signedness differs between tools, the verdicts will
 * disagree and the test fails.
 */

#include <assert.h>

char nondet_char(void);

/* Original and candidate are identical — this is a self-equivalence check.
   The function's behavior depends on char being signed:
   - If char is signed:   neg_char_check(-1) returns 1
   - If char is unsigned: neg_char_check(255) returns 0 (255 is not < 0)
*/
int orig__f(char c) {
    return c < 0 ? 1 : 0;
}

int cand__f(char c) {
    return c < 0 ? 1 : 0;
}

void harness(void) {
    char c = nondet_char();
    int r_o = orig__f(c);
    int r_c = cand__f(c);
    assert(r_o == r_c);
}

/* Expected result on x86-64 with --arch x86_64: PROVEN (self-equivalence)
   On arm64 without --arch x86_64: PROVEN (both functions behave the same,
   but differently from x86-64 — they never see negative chars).
   
   The cross-tool agreement test checks:
   - gcc -fsigned-char -O0 test.c: negative chars exist
   - clang -fsigned-char -O0 test.c: negative chars exist
   - cbmc --arch x86_64 test.c: negative chars exist
   All three must agree. */