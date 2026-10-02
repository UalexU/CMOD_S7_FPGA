/*
 * tmag5170_max31865_cmods7.c
 * TMAG5170 Hall sensor  (axi_quad_spi_0, SPI mode 0)
 * MAX31865 RTD frontend (axi_quad_spi_1, SPI mode 3)
 * Cmod S7-25 / MicroBlaze.
 *
 * Two cores rather than one core with two slaves, because the devices need
 * different SPI modes and CPOL/CPHA is a per-core setting. Each core is
 * configured once at boot and never changes.
 *
 * Vivado, per core: 1 slave, 8-bit transaction width, STARTUP primitive
 * off, ext_spi_clk and s_axi_aresetn connected, AND AN ASSIGNED ADDRESS.
 * A core with no address is silently omitted from xparameters.h.
 *
 * Wiring: TMAG5170 <- spi_{sclk,mosi,miso,ss}_0
 *         MAX31865 <- spi_{sclk,mosi,miso,ss}_1
 *
 * The UART is bidirectional. Host commands arrive as one line each and are
 * polled between samples, so nothing here ever blocks waiting for input:
 *
 *   R <hz>    main-loop rate, 1..5000 Hz
 *   A <mult>  TMAG averaging, 1 2 4 8 16 32
 *   G <mt>    magnetic range, 25 50 100
 *   P <0|1>   pause / resume streaming
 *   Z         re-send the CONFIG line
 *
 * Every accepted command re-sends CONFIG, so the host displays what the
 * board is actually doing rather than what it was asked to do.
 */

#include "xparameters.h"
#include "xspi.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "sleep.h"
#include <bspconfig.h>

/* ===================== build options ===================== */

/* 1 = checkpoints, register dumps and raw frames on the UART.
 * Each checkpoint prints BEFORE the thing it names, so whatever printed
 * last is the operation that hung.
 *
 * Leave this at 0 for any rate above a few Hz. Each sample's debug output
 * is several hundred characters, which at 115200 baud takes longer than
 * the sample period itself -- the loop then runs at the speed of the
 * debug text and the rate command appears to do nothing. */
#define DEBUG_MODE          0

/* 1 = CSV for the GUI.  0 = labelled text for a human. */
#define OUTPUT_CSV          1

/* Boot rate. Runtime-settable afterwards with 'R'. */
#define SAMPLE_PERIOD_US    200000

#define MIN_RATE_HZ         1
#define MAX_RATE_HZ         5000

/* SPI clock as configured in Vivado (ext_spi_clk / (2 * ratio)), in kHz.
 * Reported to the host so it can work out the SPI ceiling. Wrong here
 * misleads that readout; it does not affect any actual transfer. */
#define SPI_SCK_KHZ         625

#if DEBUG_MODE
  #define DBG(...)  xil_printf(__VA_ARGS__)
#else
  #define DBG(...)  ((void)0)
#endif

/* ===================== SPI cores ===================== */

#define SPI_BASE_TMAG   XPAR_AXI_QUAD_SPI_0_BASEADDR
#define SPI_BASE_RTD    XPAR_AXI_QUAD_SPI_1_BASEADDR

#define SPI_CS          0x01    /* one slave per core, so always bit 0 */
#define SPI_NO_CS       0x00

#define OPTS_TMAG   (XSP_MASTER_OPTION | XSP_MANUAL_SSELECT_OPTION)
#define OPTS_RTD    (XSP_MASTER_OPTION | XSP_MANUAL_SSELECT_OPTION | \
                     XSP_CLK_ACTIVE_LOW_OPTION | XSP_CLK_PHASE_1_OPTION)

/* ===================== UART (command channel) ===================== */

/* Registers are read directly rather than through the XUartLite driver so
 * nothing here can disturb the BSP's stdout path -- xil_printf keeps the
 * transmit side entirely to itself. If your block is named differently,
 * this is the one line to change. */
#define UART_BASE           XPAR_AXI_UARTLITE_0_BASEADDR
#define UART_RX_FIFO        0x00
#define UART_STATUS         0x08
#define UART_SR_RX_VALID    0x01

#define CMD_MAX             24

/* ===================== TMAG5170 ===================== */

#define REG_DEVICE_CONFIG   0x00
#define REG_SENSOR_CONFIG   0x01
#define REG_X_CH_RESULT     0x09
#define REG_Y_CH_RESULT     0x0A
#define REG_Z_CH_RESULT     0x0B
#define REG_TEMP_RESULT     0x0C

#define CMD_DISABLE_CRC     0x0F000407  /* datasheet Sec 7.5.2.5 */

/* RANGE_CODE goes to the sensor, RANGE_MT_X100 scales the result. These
 * two must agree or readings are silently wrong, which is why 'G' changes
 * them together and nothing else may write either one.
 *   0x1 = +/-25 mT (2500)   0x0 = +/-50 mT (5000)   0x2 = +/-100 mT (10000) */
#define RANGE_CODE          0x2
#define RANGE_MT_X100       10000

#define CONV_AVG            0x5     /* 0h=1x .. 5h=32x internal averaging */

/* Temperature: T = 25 + (raw - 17522) / 60, kept x100 (no FPU). */
#define T_ADC_T0            17522
#define T_ADC_RES           60

/* ===================== MAX31865 ===================== */

#define RTD_REG_COUNT       9       /* address byte + registers 00h..07h */
#define RTD_ADDR_READ       0x00
#define RTD_ADDR_WRITE      0x80    /* write address = read address | 0x80 */

#define RTD_VBIAS_ON        0x80
#define RTD_AUTO_CONV       0x40
#define RTD_3WIRE           0x10    /* 0 = 2- or 4-wire */
#define RTD_FAULT_CLEAR     0x02
#define RTD_50HZ            0x01    /* 0 = 60 Hz notch */

/* Set to RTD_3WIRE for a 3-wire probe. The wrong setting does not fault --
 * it reads consistently wrong, which is harder to notice.
 *
 * (This is now actually used. It previously sat here while RTD_CONFIG
 * hard-coded RTD_3WIRE, so changing it had no effect at all.) */
#define RTD_WIRE_MODE       0x00

#define RTD_CONFIG      (RTD_VBIAS_ON | RTD_AUTO_CONV | RTD_FAULT_CLEAR | \
                         RTD_WIRE_MODE)

#define RTD_CONV_HZ     ((RTD_CONFIG & RTD_50HZ) ? 50 : 60)

static XSpi SpiTmag;
static XSpi SpiRtd;

/* ===================== runtime state ===================== */

/* Everything the host can change. Defaults are the compile-time settings
 * above, so a board that is never sent a command behaves exactly as it
 * did before the command channel existed. */
static u32 sample_period_us = SAMPLE_PERIOD_US;
static u8  conv_avg_code    = CONV_AVG;      /* 0h..5h */
static u8  range_code       = RANGE_CODE;
static s32 range_mt_x100    = RANGE_MT_X100;
static int streaming        = 1;

static char cmd_buf[CMD_MAX];
static int  cmd_len;

/* Averaging code 0h..5h as a multiplier: 1x..32x. */
static u16 avg_mult(void)
{
    return (u16)(1u << conv_avg_code);
}

/* ===================== SPI plumbing ===================== */

/* The two registers that explain a hang. CR bit 8 is "master transaction
 * inhibit" -- if it is still 1 the core never started and no transfer will
 * ever complete. SR bit 2 is TX FIFO empty. */
static void spi_dump(XSpi *dev, const char *label)
{
    DBG("#   %s: base=0x%08X ss_bits=%d CR=0x%04X SR=0x%04X\r\n",
        label, (unsigned)dev->BaseAddr, dev->NumSlaveBits,
        (unsigned)XSpi_ReadReg(dev->BaseAddr, XSP_CR_OFFSET),
        (unsigned)XSpi_GetStatusReg(dev));
}

/* One transfer, CS held low throughout. */
static int spi_burst(XSpi *dev, const char *label, u8 *tx, u8 *rx, int len)
{
    int status;

    DBG("#   %s: transfer %d bytes...\r\n", label, len);

    XSpi_SetSlaveSelect(dev, SPI_CS);
    status = XSpi_Transfer(dev, tx, rx, len);
    XSpi_SetSlaveSelect(dev, SPI_NO_CS);

    if (status != XST_SUCCESS) {
        xil_printf("# %s: transfer failed (%d)\r\n", label, status);
    } else {
        DBG("#   %s: done\r\n", label);
    }
    return status;
}

/* Bring one core up. Polled operation: without the interrupt disable,
 * Transfer() returns success having only kicked off an interrupt-driven
 * transfer that nothing services, leaving rx[] zeroed. */
static int spi_setup(XSpi *dev, u32 base, u32 opts, const char *label)
{
    XSpi_Config *cfg;
    int status;

    DBG("# %s: LookupConfig(0x%08X)\r\n", label, (unsigned)base);
    cfg = XSpi_LookupConfig(base);
    if (cfg == NULL) {
        xil_printf("# %s: no config -- is the core addressed in Vivado?\r\n",
                   label);
        return XST_FAILURE;
    }

    DBG("# %s: CfgInitialize\r\n", label);
    status = XSpi_CfgInitialize(dev, cfg, cfg->BaseAddress);
    if (status != XST_SUCCESS) {
        xil_printf("# %s: CfgInitialize failed (%d)\r\n", label, status);
        return status;
    }

    DBG("# %s: SetOptions 0x%02X\r\n", label, (unsigned)opts);
    status = XSpi_SetOptions(dev, opts);
    if (status != XST_SUCCESS) {
        xil_printf("# %s: SetOptions failed (%d)\r\n", label, status);
        return status;
    }

    DBG("# %s: Start\r\n", label);
    XSpi_Start(dev);
    XSpi_IntrGlobalDisable(dev);
    XSpi_SetSlaveSelect(dev, SPI_NO_CS);

    spi_dump(dev, label);

    if (dev->DataWidth != XSP_DATAWIDTH_BYTE) {
        xil_printf("# %s: transaction width %d bits, expected 8\r\n",
                   label, dev->DataWidth);
    }
    return XST_SUCCESS;
}

/* ===================== TMAG5170 ===================== */

/* Frame: [31] R/W | [30:24] address | [23:8] data | [7:4] CMD | [3:0] CRC */
static u32 tmag_frame(u8 rw, u8 addr, u16 data)
{
    return ((u32)(rw & 1) << 31) | ((u32)(addr & 0x7F) << 24) |
           ((u32)data << 8);
}

/* 32 bits out, 32 back. The sensor counts SCK edges and rejects any frame
 * that is not exactly 32 clocks long -- hence manual CS. */
static u32 tmag_xfer(u32 frame)
{
    u8 tx[4], rx[4] = { 0 };

    tx[0] = (u8)(frame >> 24);
    tx[1] = (u8)(frame >> 16);
    tx[2] = (u8)(frame >> 8);
    tx[3] = (u8)frame;

    if (spi_burst(&SpiTmag, "tmag", tx, rx, 4) != XST_SUCCESS) {
        return 0;
    }

    DBG("#   tmag tx %02X %02X %02X %02X -> rx %02X %02X %02X %02X\r\n",
        tx[0], tx[1], tx[2], tx[3], rx[0], rx[1], rx[2], rx[3]);

    return ((u32)rx[0] << 24) | ((u32)rx[1] << 16) |
           ((u32)rx[2] << 8) | rx[3];
}

static void tmag_write(u8 addr, u16 data)
{
    tmag_xfer(tmag_frame(0, addr, data));
}

/* Reply is 8 status | 16 data | 4 status | 4 CRC */
static u16 tmag_read(u8 addr)
{
    return (u16)((tmag_xfer(tmag_frame(1, addr, 0)) >> 8) & 0xFFFF);
}

/* Datasheet Eq 1 scaled x100: B = raw * RANGE / 32768.
 * Reads range_mt_x100 rather than the macro, so a range change takes
 * effect on the very next sample. */
static s32 tmag_mT_x100(u16 raw)
{
    return ((s32)(s16)raw * range_mt_x100) / 32768;
}

/* Datasheet Eq 3 scaled x100. TEMP_RESULT is unsigned binary, not 2's
 * complement -- the sign comes from the subtraction. */
static s32 tmag_degC_x100(u16 raw)
{
    return 2500 + (((s32)raw - T_ADC_T0) * 100) / T_ADC_RES;
}

/* Confirms a config write took. Returns 1 on mismatch. */
static int tmag_verify(u8 addr, u16 want, const char *label)
{
    u16 got = tmag_read(addr);

    if (got != want) {
        xil_printf("# %s readback 0x%04X, expected 0x%04X\r\n",
                   label, got, want);
        return 1;
    }
    DBG("# %s ok (0x%04X)\r\n", label, got);
    return 0;
}

/* SENSOR_CONFIG: MAG_CH_EN=7h (XYZ) in bits 9:6, plus range per axis. */
static u16 sensor_config_word(void)
{
    return (u16)(0x01C0 | ((u16)range_code << 4) |
                 ((u16)range_code << 2) | range_code);
}

/* DEVICE_CONFIG in ONE write -- a register write replaces all 16 bits, so
 * setting the temperature bits in a later write would clobber CONV_AVG.
 *   bits 14:12 CONV_AVG | bits 6:4 OPERATING_MODE=2h (active)
 *   bit 3 T_CH_EN       | bit 2 T_RATE (temp converts once per set) */
static u16 device_config_word(void)
{
    return (u16)(((u16)conv_avg_code << 12) | 0x0020 | 0x0008 | 0x0004);
}

static int tmag_apply_sensor_config(void)
{
    u16 want = sensor_config_word();

    tmag_write(REG_SENSOR_CONFIG, want);
    return tmag_verify(REG_SENSOR_CONFIG, want, "SENSOR_CONFIG");
}

static int tmag_apply_device_config(void)
{
    u16 want = device_config_word();

    tmag_write(REG_DEVICE_CONFIG, want);
    usleep(1000);
    return tmag_verify(REG_DEVICE_CONFIG, want, "DEVICE_CONFIG");
}

/* ===================== MAX31865 ===================== */

static void rtd_configure(void)
{
    u8 tx[2] = { RTD_ADDR_WRITE, RTD_CONFIG };
    u8 rx[2] = { 0 };

    /* Out of reset the config register is 00h -- bias off, ADC off -- so
     * the RTD registers never fill in. */
    spi_burst(&SpiRtd, "rtd cfg", tx, rx, 2);
}

/* Reads the whole block; the chip auto-increments, so rx[1]..rx[8] hold
 * registers 00h..07h. The RTD value is a 15-bit code in D15..D1 with the
 * fault flag in D0 -- read the flag before shifting, the shift discards it.
 * Linear approximation (datasheet p.11): T = code/32 - 256. */
static s32 rtd_degC_x100(int *fault)
{
    u8 tx[RTD_REG_COUNT] = { RTD_ADDR_READ };
    u8 rx[RTD_REG_COUNT] = { 0 };
    int adc;

    *fault = 0;

    if (spi_burst(&SpiRtd, "rtd", tx, rx, RTD_REG_COUNT) != XST_SUCCESS) {
        *fault = 1;
        return 0;
    }

#if DEBUG_MODE
    {
        int i;
        DBG("#   rtd regs:");
        for (i = 0; i < RTD_REG_COUNT; i++) {
            DBG(" %02X", rx[i]);
        }
        DBG("\r\n");
        /* Power-on constants: [4][5] must be FF FF and [6][7] 00 00. If
         * they are not, byte alignment is wrong and nothing else holds. */
        if (rx[4] != 0xFF || rx[5] != 0xFF || rx[6] || rx[7]) {
            xil_printf("# rtd ALIGNMENT SUSPECT -- want FF FF 00 00 "
                       "at [4][5][6][7]\r\n");
        }
    }
#endif

    *fault = rx[3] & 0x01;
    adc = (rx[2] << 7) | (rx[3] >> 1);

    return ((s32)adc * 100) / 32 - 25600;
}

/* ===================== output ===================== */

/* Sign handled separately: -50/100 is 0 in C, which drops the minus. */
static void print_x100(s32 v)
{
    s32 whole = v / 100;
    s32 frac = v % 100;

    if (frac < 0) {
        frac = -frac;
    }
    if (v < 0 && whole == 0) {
        xil_printf("-0.%02d", (int)frac);
    } else {
        xil_printf("%d.%02d", (int)whole, (int)frac);
    }
}

/* Exact integer sqrt -- no FPU, no libm. */
static u32 isqrt_u32(u32 n)
{
    u32 rem = 0, root = 0;
    int i;

    for (i = 0; i < 16; i++) {
        root <<= 1;
        rem = (rem << 2) | (n >> 30);
        n <<= 2;
        if (root < rem) {
            root++;
            rem -= root;
            root++;
        }
    }
    return root >> 1;
}

/* Sent at startup and after every accepted command, so the host never has
 * to assume a command took effect -- it reads back what the board is
 * actually doing. One line, ~70 characters, ~6 ms on the wire at 115200:
 * it costs nothing because it is not per-sample. */
static void report_config(void)
{
    xil_printf("# CONFIG conv_avg=%d range_mt=%d axes=3 temp=1 rtd_hz=%d "
               "period_us=%d sck_khz=%d\r\n",
               (int)avg_mult(), (int)(range_mt_x100 / 100), RTD_CONV_HZ,
               (int)sample_period_us, SPI_SCK_KHZ);
}

/* ===================== command channel ===================== */

/* Non-blocking: returns -1 when the RX FIFO is empty. Reading the register
 * directly avoids any interaction with the BSP's stdout path. */
static int uart_getchar(void)
{
    if (Xil_In32(UART_BASE + UART_STATUS) & UART_SR_RX_VALID) {
        return (int)(Xil_In32(UART_BASE + UART_RX_FIFO) & 0xFF);
    }
    return -1;
}

/* Decimal integer after the command letter. Returns -1 if there isn't one,
 * so every caller can reject a malformed line the same way. */
static int parse_int(const char *s)
{
    int value = 0;
    int digits = 0;

    while (*s == ' ' || *s == '\t') {
        s++;
    }
    while (*s >= '0' && *s <= '9') {
        value = value * 10 + (*s - '0');
        s++;
        digits++;
        if (value > 1000000) {
            return -1;
        }
    }
    return digits ? value : -1;
}

/* mult -> CONV_AVG code, or -1 if it is not a supported power of two. */
static int avg_code_for(int mult)
{
    int code = 0;

    if (mult < 1 || mult > 32) {
        return -1;
    }
    while ((mult & 1) == 0) {
        mult >>= 1;
        code++;
    }
    return (mult == 1) ? code : -1;    /* rejects 3, 5, 6, ... */
}

static void handle_command(char *line)
{
    int arg = parse_int(line + 1);
    int code;

    switch (line[0]) {

    case 'R':                          /* main-loop rate in Hz */
        if (arg < MIN_RATE_HZ || arg > MAX_RATE_HZ) {
            xil_printf("# ERR R: want %d..%d Hz\r\n",
                       MIN_RATE_HZ, MAX_RATE_HZ);
            return;
        }
        sample_period_us = (u32)(1000000 / arg);
#if DEBUG_MODE
        if (arg > 20) {
            xil_printf("# WARN DEBUG_MODE=1 prints hundreds of bytes per "
                       "sample; the loop cannot reach %d Hz\r\n", arg);
        }
#endif
        break;

    case 'A':                          /* TMAG averaging multiplier */
        code = avg_code_for(arg);
        if (code < 0) {
            xil_printf("# ERR A: want 1 2 4 8 16 or 32\r\n");
            return;
        }
        conv_avg_code = (u8)code;
        if (tmag_apply_device_config()) {
            xil_printf("# ERR A: DEVICE_CONFIG did not take\r\n");
            return;
        }
        break;

    case 'G':                          /* magnetic full scale in mT */
        /* Code and scale factor move together or readings are silently
         * wrong -- this switch is the only place either one changes. */
        if (arg == 25) {
            range_code = 0x1;
            range_mt_x100 = 2500;
        } else if (arg == 50) {
            range_code = 0x0;
            range_mt_x100 = 5000;
        } else if (arg == 100) {
            range_code = 0x2;
            range_mt_x100 = 10000;
        } else {
            xil_printf("# ERR G: want 25, 50 or 100 mT\r\n");
            return;
        }
        if (tmag_apply_sensor_config()) {
            xil_printf("# ERR G: SENSOR_CONFIG did not take\r\n");
            return;
        }
        break;

    case 'P':                          /* pause / resume streaming */
        if (arg != 0 && arg != 1) {
            xil_printf("# ERR P: want 0 or 1\r\n");
            return;
        }
        streaming = arg;
        break;

    case 'Z':                          /* just re-report */
        break;

    default:
        xil_printf("# ERR unknown command '%c'\r\n", line[0]);
        return;
    }

    xil_printf("# ACK %s\r\n", line);
    report_config();
}

/* Drains whatever has arrived and acts on complete lines only. Called
 * between samples, so a command never interrupts a conversion. */
static void poll_commands(void)
{
    int c;

    while ((c = uart_getchar()) >= 0) {
        if (c == '\r' || c == '\n') {
            if (cmd_len > 0) {
                cmd_buf[cmd_len] = '\0';
                handle_command(cmd_buf);
                cmd_len = 0;
            }
        } else if (cmd_len < CMD_MAX - 1) {
            cmd_buf[cmd_len++] = (char)c;
        } else {
            /* Overlong line: drop the whole thing rather than act on a
             * fragment that happens to start with a valid letter. */
            cmd_len = 0;
        }
    }
}

/* ===================== main ===================== */

int main(void)
{
    u16 rawX, rawY, rawZ, rawT;
    s32 bx, by, bz, dieC, rtdC;
    u32 mag;
    int fault;

    DBG("\r\n# --- boot ---\r\n");

    if (spi_setup(&SpiTmag, SPI_BASE_TMAG, OPTS_TMAG, "tmag core")
            != XST_SUCCESS) {
        return -1;
    }
    if (spi_setup(&SpiRtd, SPI_BASE_RTD, OPTS_RTD, "rtd core")
            != XST_SUCCESS) {
        return -1;
    }

    /* Identical base addresses mean both XPAR names point at one core --
     * easy to do after a block rename, and it fails as garbage, not error. */
    if (SpiTmag.BaseAddr == SpiRtd.BaseAddr) {
        xil_printf("# BOTH CORES AT 0x%08X -- check xparameters.h\r\n",
                   (unsigned)SpiTmag.BaseAddr);
        return -1;
    }

    DBG("# tmag: disable CRC\r\n");
    tmag_xfer(CMD_DISABLE_CRC);
    usleep(1000);

    DBG("# tmag: SENSOR_CONFIG\r\n");
    tmag_apply_sensor_config();

    DBG("# tmag: DEVICE_CONFIG\r\n");
    tmag_apply_device_config();

    DBG("# rtd: configure\r\n");
    rtd_configure();

    /* One conversion period so the first reading is real, not power-on 0. */
    usleep(1000000 / RTD_CONV_HZ);

    report_config();

#if OUTPUT_CSV
    xil_printf("Bx,By,Bz,Bmag,DieC,RtdC\r\n");
#else
    xil_printf("\r\nTMAG5170 + MAX31865 on Cmod S7\r\n");
#endif

    while (1) {
        /* Commands first: a paused board must still answer, and at a slow
         * rate this is the only thing keeping the link responsive. */
        poll_commands();

        if (!streaming) {
            usleep(20000);
            continue;
        }

        rawX = tmag_read(REG_X_CH_RESULT);
        rawY = tmag_read(REG_Y_CH_RESULT);
        rawZ = tmag_read(REG_Z_CH_RESULT);
        rawT = tmag_read(REG_TEMP_RESULT);
        rtdC = rtd_degC_x100(&fault);

        bx = tmag_mT_x100(rawX);
        by = tmag_mT_x100(rawY);
        bz = tmag_mT_x100(rawZ);
        dieC = tmag_degC_x100(rawT);
        mag = isqrt_u32((u32)(bx * bx) + (u32)(by * by) + (u32)(bz * bz));

        if (fault) {
            xil_printf("# rtd FAULT -- read register 07h for the cause\r\n");
        }

#if OUTPUT_CSV
        print_x100(bx);         xil_printf(", ");
        print_x100(by);         xil_printf(", ");
        print_x100(bz);         xil_printf(", ");
        print_x100((s32)mag);   xil_printf(", ");
        print_x100(dieC);       xil_printf(", ");
        print_x100(rtdC);       xil_printf("\r\n");
#else
        xil_printf("raw %04X %04X %04X %04X | B ", rawX, rawY, rawZ, rawT);
        print_x100(bx);        xil_printf(" ");
        print_x100(by);        xil_printf(" ");
        print_x100(bz);        xil_printf(" mT | |B| ");
        print_x100((s32)mag);  xil_printf(" mT | die ");
        print_x100(dieC);      xil_printf(" C | rtd ");
        print_x100(rtdC);      xil_printf(" C\r\n");
#endif

        usleep(sample_period_us);
    }

    return 0;
}
