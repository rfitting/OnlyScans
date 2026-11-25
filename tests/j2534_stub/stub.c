/*
 * OnlyScans test J2534 driver (CI only).
 *
 * A minimal SAE J2534 v04.04 DLL that pretends to be a CAN-only pass-thru device plugged
 * into a car with one stored code (P0301) and VIN JTMBF4DV1AD000000. The build workflow
 * compiles it as 32-bit, registers it under PassThruSupport.04.04 like a real driver
 * installer would, and scans through it with OnlyScans.exe. That exercises the real
 * Windows path: registry discovery, loading the DLL, and every PassThru* call.
 */
#include <windows.h>
#include <string.h>

#define STATUS_NOERROR       0x00
#define ERR_NOT_SUPPORTED    0x01
#define ERR_INVALID_CHANNEL  0x02
#define ERR_INVALID_PROTOCOL 0x03
#define ERR_EXCEEDED_LIMIT   0x0C
#define ERR_BUFFER_EMPTY     0x10
#define ISO15765             6
#define FLOW_CONTROL_FILTER  3
#define READ_VBATT           3
#define CLEAR_RX_BUFFER      8

typedef struct {
    unsigned long ProtocolID, RxStatus, TxFlags, Timestamp, DataSize, ExtraDataIndex;
    unsigned char Data[4128];
} PASSTHRU_MSG;

static PASSTHRU_MSG queue[32];
static int q_head = 0, q_len = 0;
static unsigned long fc_ids[16];
static int fc_used[16];

static void push(unsigned long id, const unsigned char *payload, unsigned long n) {
    PASSTHRU_MSG *m;
    if (q_len >= 32) return;
    m = &queue[(q_head + q_len++) % 32];
    memset(m, 0, sizeof(*m));
    m->ProtocolID = ISO15765;
    m->DataSize = 4 + n;
    m->Data[0] = (unsigned char)(id >> 24); m->Data[1] = (unsigned char)(id >> 16);
    m->Data[2] = (unsigned char)(id >> 8);  m->Data[3] = (unsigned char)id;
    memcpy(m->Data + 4, payload, n);
}

static int has_filter(unsigned long id) {
    int i;
    for (i = 0; i < 16; i++) if (fc_used[i] && fc_ids[i] == id) return 1;
    return 0;
}

#define REPLY(...) do { static const unsigned char r[] = {__VA_ARGS__}; push(0x7E8, r, sizeof(r)); return; } while (0)

static void ecu(unsigned long id, const unsigned char *d, unsigned long n) {
    static const unsigned char vin[] = {0x49, 0x02, 0x01, 'J','T','M','B','F','4','D','V','1','A','D','0','0','0','0','0','0'};
    if (!has_filter(0x7E8) || (id != 0x7DF && id != 0x7E0) || n == 0) return;
    if (d[0] == 0x01 && n >= 2) {
        switch (d[1]) {
        case 0x00: REPLY(0x41, 0x00, 0x18, 0x10, 0x00, 0x00);   /* PIDs 04, 05, 0C */
        case 0x01: REPLY(0x41, 0x01, 0x81, 0x07, 0x65, 0x00);   /* MIL on, 1 code */
        case 0x04: REPLY(0x41, 0x04, 0x59);
        case 0x05: REPLY(0x41, 0x05, 0x7B);
        case 0x0C: REPLY(0x41, 0x0C, 0x0B, 0x40);
        }
        return;
    }
    if (d[0] == 0x03) REPLY(0x43, 0x01, 0x03, 0x01);            /* P0301 */
    if (d[0] == 0x07) REPLY(0x47, 0x00);
    if (d[0] == 0x0A) REPLY(0x4A, 0x00);
    if (d[0] == 0x09 && n >= 2 && d[1] == 0x02) { push(0x7E8, vin, sizeof(vin)); return; }
    if (d[0] == 0x02 && n >= 3 && d[1] == 0x02) REPLY(0x42, 0x02, 0x00, 0x03, 0x01);
    if (id == 0x7E0) {                                          /* physical requests */
        if (d[0] == 0x13) REPLY(0x53, 0x01, 0x03, 0x01);
        if (d[0] == 0x19 || d[0] == 0x18 || d[0] == 0x3E) {
            unsigned char neg[3];
            neg[0] = 0x7F; neg[1] = d[0]; neg[2] = 0x11;            /* service not supported */
            push(0x7E8, neg, 3);
        }
    }
}

long __stdcall PassThruOpen(void *name, unsigned long *dev) { (void)name; *dev = 1; return STATUS_NOERROR; }
long __stdcall PassThruClose(unsigned long dev) { (void)dev; return STATUS_NOERROR; }

long __stdcall PassThruConnect(unsigned long dev, unsigned long proto, unsigned long flags,
                               unsigned long baud, unsigned long *ch) {
    (void)dev;
    if (proto != ISO15765) return ERR_INVALID_PROTOCOL;          /* CAN-only tool */
    if ((flags & 0x100) || baud != 500000) return ERR_NOT_SUPPORTED;
    memset(fc_used, 0, sizeof(fc_used));
    q_len = 0;
    *ch = 1;
    return STATUS_NOERROR;
}

long __stdcall PassThruDisconnect(unsigned long ch) { (void)ch; return STATUS_NOERROR; }

long __stdcall PassThruStartMsgFilter(unsigned long ch, unsigned long type, PASSTHRU_MSG *mask,
                                      PASSTHRU_MSG *pattern, PASSTHRU_MSG *flow, unsigned long *id) {
    int i, used = 0;
    (void)mask; (void)flow;
    if (ch != 1) return ERR_INVALID_CHANNEL;
    for (i = 0; i < 16; i++) used += fc_used[i];
    if (used >= 10) return ERR_EXCEEDED_LIMIT;                   /* typical device limit */
    for (i = 0; i < 16; i++) if (!fc_used[i]) {
        fc_used[i] = 1;
        fc_ids[i] = (type == FLOW_CONTROL_FILTER && pattern && pattern->DataSize >= 4)
            ? ((unsigned long)pattern->Data[0] << 24 | (unsigned long)pattern->Data[1] << 16 |
               (unsigned long)pattern->Data[2] << 8 | pattern->Data[3]) : 0;
        *id = (unsigned long)i + 1;
        return STATUS_NOERROR;
    }
    return ERR_EXCEEDED_LIMIT;
}

long __stdcall PassThruStopMsgFilter(unsigned long ch, unsigned long id) {
    (void)ch;
    if (id >= 1 && id <= 16) fc_used[id - 1] = 0;
    return STATUS_NOERROR;
}

long __stdcall PassThruWriteMsgs(unsigned long ch, PASSTHRU_MSG *msgs, unsigned long *num,
                                 unsigned long timeout) {
    unsigned long i;
    (void)timeout;
    if (ch != 1) return ERR_INVALID_CHANNEL;
    for (i = 0; i < *num; i++) {
        PASSTHRU_MSG *m = &msgs[i];
        unsigned long id;
        if (m->DataSize < 4) continue;
        id = (unsigned long)m->Data[0] << 24 | (unsigned long)m->Data[1] << 16 |
             (unsigned long)m->Data[2] << 8 | m->Data[3];
        ecu(id, m->Data + 4, m->DataSize - 4);
    }
    return STATUS_NOERROR;
}

long __stdcall PassThruReadMsgs(unsigned long ch, PASSTHRU_MSG *msgs, unsigned long *num,
                                unsigned long timeout) {
    unsigned long want = *num, got = 0;
    if (ch != 1) return ERR_INVALID_CHANNEL;
    while (got < want && q_len > 0) {
        msgs[got++] = queue[q_head];
        q_head = (q_head + 1) % 32;
        q_len--;
    }
    *num = got;
    if (got) return STATUS_NOERROR;
    Sleep(timeout < 20 ? timeout : 20);
    return ERR_BUFFER_EMPTY;
}

long __stdcall PassThruIoctl(unsigned long ch, unsigned long ioctl, void *in, void *out) {
    (void)ch; (void)in;
    if (ioctl == READ_VBATT && out) *(unsigned long *)out = 12600;
    if (ioctl == CLEAR_RX_BUFFER) q_len = 0;
    return STATUS_NOERROR;
}

long __stdcall PassThruReadVersion(unsigned long dev, char *fw, char *dll, char *api) {
    (void)dev;
    strcpy(fw, "1.0"); strcpy(dll, "1.0"); strcpy(api, "04.04");
    return STATUS_NOERROR;
}

long __stdcall PassThruGetLastError(char *desc) { strcpy(desc, "OnlyScans test driver"); return STATUS_NOERROR; }

/* Unused by OnlyScans but part of the standard export list. */
long __stdcall PassThruStartPeriodicMsg(unsigned long ch, PASSTHRU_MSG *m, unsigned long *id,
                                        unsigned long t) { (void)ch; (void)m; (void)id; (void)t; return ERR_NOT_SUPPORTED; }
long __stdcall PassThruStopPeriodicMsg(unsigned long ch, unsigned long id) { (void)ch; (void)id; return ERR_NOT_SUPPORTED; }
long __stdcall PassThruSetProgrammingVoltage(unsigned long dev, unsigned long pin, unsigned long v) { (void)dev; (void)pin; (void)v; return ERR_NOT_SUPPORTED; }
