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
 */

#include "xparameters.h"
#include "xspi.h"
#include "xil_printf.h"
#include "sleep.h"
#include <bspconfig.h>

/* ===================== build options ===================== */

/* 1 = checkpoints, register dumps and raw frames on the UART.
 * Each checkpoint prints BEFORE the thing it names, so whatever printed
 * last is the operation that hung. */
#define DEBUG_MODE          1

/* 1 = CSV for the GUI.  0 = labelled text for a human. */
#define OUTPUT_CSV          1

#define SAMPLE_PERIOD_US    200000

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

/* ===================== TMAG5170 ===================== */

#define REG_DEVICE_CONFIG   0x00
#define REG_SENSOR_CONFIG   0x01
#define REG_X_CH_RESULT     0x09
#define REG_Y_CH_RESULT     0x0A
#define REG_Z_CH_RESULT     0x0B
#define REG_TEMP_RESULT     0x0C

#define CMD_DISABLE_CRC     0x0F000407  /* datasheet Sec 7.5.2.5 */

/* RANGE_CODE goes to the sensor, RANGE_MT_X100 scales the result. These
 * two must agree or readings are silently wrong.
 *   0x1 = +/-25 mT (2500)   0x0 = +/-50 mT (5000)   0x2 = +/-100 mT (10000) */
#define RANGE_CODE          0x2
#define RANGE_MT_X100       10000

#define CONV_AVG            0x5     /* 0h=1x .. 5h=32x internal averaging */

/* SENSOR_CONFIG: MAG_CH_EN=7h (XYZ) in bits 9:6, plus range per axis. */
#define SENSOR_CONFIG_XYZ   (0x01C0 | (RANGE_CODE << 4) \
                                    | (RANGE_CODE << 2) | RANGE_CODE)

/* DEVICE_CONFIG in ONE write -- a register write replaces all 16 bits, so
 * setting the temperature bits in a later write would clobber CONV_AVG.
 *   bits 14:12 CONV_AVG | bits 6:4 OPERATING_MODE=2h (active)
 *   bit 3 T_CH_EN       | bit 2 T_RATE (temp converts once per set) */
#define DEVICE_CONFIG_ACTIVE (((u16)CONV_AVG << 12) | 0x0020 | 0x0008 | 0x0004)

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
 * it reads consistently wrong, which is harder to notice. */
#define RTD_WIRE_MODE       0x00

#define RTD_CONFIG      (RTD_VBIAS_ON | RTD_AUTO_CONV | RTD_FAULT_CLEAR | \
                         RTD_3WIRE)

                         
#define RTD_CONV_HZ     ((RTD_CONFIG & RTD_50HZ) ? 50 : 60)

/* Conversion averaging as a multiplier: CONV_AVG 0h..5h -> 1x..32x. */
#define AVG_MULT            (1 << CONV_AVG)

static XSpi SpiTmag;
static XSpi SpiRtd;

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

/* Datasheet Eq 1 scaled x100: B = raw * RANGE / 32768 */
static s32 tmag_mT_x100(u16 raw)
{
    return ((s32)(s16)raw * RANGE_MT_X100) / 32768;
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

/* Sent once at startup so the host knows how the sensors are configured.
 * One line, ~50 characters, ~4 ms on the wire at 115200 -- it costs nothing
 * because it is never repeated. Only per-sample output affects throughput. */
static void report_config(void)
{
    xil_printf("# CONFIG conv_avg=%d range_mt=%d axes=3 temp=1 rtd_hz=%d\r\n",
               AVG_MULT, RANGE_MT_X100 / 100, RTD_CONV_HZ);
}


static void uart_config_info(void){
    
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
    tmag_write(REG_SENSOR_CONFIG, SENSOR_CONFIG_XYZ);
    tmag_verify(REG_SENSOR_CONFIG, SENSOR_CONFIG_XYZ, "SENSOR_CONFIG");

    DBG("# tmag: DEVICE_CONFIG\r\n");
    tmag_write(REG_DEVICE_CONFIG, DEVICE_CONFIG_ACTIVE);
    usleep(1000);
    tmag_verify(REG_DEVICE_CONFIG, DEVICE_CONFIG_ACTIVE, "DEVICE_CONFIG");

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

        usleep(SAMPLE_PERIOD_US);
    }

    return 0;
}