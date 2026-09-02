/*
 * tmag5170_cmods7.c
 * TMAG5170 3D Hall sensor over AXI Quad SPI, Cmod S7-25 / MicroBlaze.
 *
 * Wiring, Pmod JA -> TMAG5170UEVM (SPI is role-named: MOSI->MOSI, not crossed):
 *   JA1 J2 -> CS      JA2 H2 -> MOSI    JA3 H4 <- MISO
 *   JA4 F3 -> SCLK    JA5 -> GND        JA6 -> 3V3
 *
 * Vivado: AXI Quad SPI in Standard mode, master, transaction width 8,
 *         1 slave, STARTUP primitive off.
 * Vitis:  BSP STDOUT = axi_uartlite_0.
 */

#include "xparameters.h"
#include "xspi.h"
#include "xil_printf.h"
#include "sleep.h"

/* 1 = "1.23, -4.56, 0.07, 4.72, 25.43"   0 = labelled human-readable */
#define OUTPUT_CSV          1

/* 1 = dump the raw bytes of every SPI transfer */
#define TMAG_DEBUG_FRAMES   0

#define SAMPLE_PERIOD_US    200000

#define SPI_LOOKUP_ARG      XPAR_AXI_QUAD_SPI_0_BASEADDR
#define SPI_SELECT_CS0      0x01    /* bitmask, not an index */
#define SPI_DESELECT_ALL    0x00

/* Register addresses, datasheet Table 7-4 */
#define REG_DEVICE_CONFIG   0x00
#define REG_SENSOR_CONFIG   0x01
#define REG_X_CH_RESULT     0x09
#define REG_Y_CH_RESULT     0x0A
#define REG_Z_CH_RESULT     0x0B
#define REG_TEMP_RESULT     0x0C
#define REG_TEST_CONFIG     0x0F

#define CMD_DISABLE_CRC     0x0F000407  /* datasheet Sec 7.5.2.5         */

/* Temperature conversion, datasheet Eq 3 + Electrical Characteristics:
 *   T = TSENS_T0 + (TADCT - TADCT0) / TADCRES
 * All values kept as x100 fixed-point integers -- the MicroBlaze has no FPU. */
#define T_SENS_T0_X100      2500    /* reference temp, 25.00 degC          */
#define T_ADC_T0            17522   /* TEMP_RESULT raw value at 25 degC    */
#define T_ADC_RES_LSB_PER_C 60      /* typical LSB per degC (58.2 .. 61.8) */

/* Magnetic full-scale range, applied to all three axes. The two lines below
 * MUST agree -- RANGE_CODE goes into the sensor, RANGE_MT_X100 scales the
 * result, and a mismatch silently gives wrong readings.
 *
 *   RANGE_CODE   TMAG5170A1    RANGE_MT_X100   16-bit step
 *      0x1        +/-25 mT         2500         0.00076 mT
 *      0x0        +/-50 mT         5000         0.00153 mT
 *      0x2       +/-100 mT        10000         0.00305 mT
 *
 * A trace that sits perfectly flat at exactly the full-scale value is
 * saturation, not a measurement -- move up a range. */
#define RANGE_CODE          0x2
#define RANGE_MT_X100       10000

/* MAG_CH_EN = 7h (XYZ) in bits 9:6, plus the same range on Z, Y and X. */
#define SENSOR_CONFIG_XYZ   (0x01C0 | ((RANGE_CODE) << 4) \
                                    | ((RANGE_CODE) << 2) \
                                    | ((RANGE_CODE) << 0))

/* CONV_AVG, DEVICE_CONFIG bits 14:12 -- how many measurements the sensor
 * averages internally before updating a result register. Higher is quieter
 * and finer (all 16 result bits become significant instead of only the top
 * 12), at the cost of how often a new result appears:
 *
 *   0h = 1x  -> 10.0 ksps      3h = 8x  -> 1.6 ksps
 *   1h = 2x  ->  5.7 ksps      4h = 16x -> 0.8 ksps
 *   2h = 4x  ->  3.1 ksps      5h = 32x -> 0.4 ksps
 *
 * (rates are for all three axes enabled). We read at 5 Hz, so even 32x
 * produces results 80x faster than we consume them. */
#define CONV_AVG            0x5

/* T_CH_EN, DEVICE_CONFIG bit 3 -- enables the temperature channel. Off by
 * default; without it TEMP_RESULT never updates and reads back as 0. */
#define T_CH_EN             0x0008

/* DEVICE_CONFIG: CONV_AVG (bits 14:12) | OPERATING_MODE=2h (bits 6:4)
 * | T_CH_EN (bit 3). Built as ONE value on purpose -- a register write
 * replaces all 16 bits, so enabling temperature in a separate later write
 * would clobber CONV_AVG back to 0. */
#define DEVICE_CONFIG_ACTIVE (((u16)(CONV_AVG) << 12) | 0x0020 | T_CH_EN)

static XSpi Spi;


/* One 32-bit frame out, 32 bits back. CS is held low across all four
 * bytes; the sensor counts SCK edges and rejects any frame that is not
 * exactly 32 clocks long. */
static u32 tmag_xfer(u32 frame)
{
    u8 tx[4], rx[4];
    int status;

    tx[0] = (u8)(frame >> 24);
    tx[1] = (u8)(frame >> 16);
    tx[2] = (u8)(frame >>  8);
    tx[3] = (u8)(frame >>  0);
    rx[0] = rx[1] = rx[2] = rx[3] = 0;

    XSpi_SetSlaveSelect(&Spi, SPI_SELECT_CS0);
    status = XSpi_Transfer(&Spi, tx, rx, 4);
    XSpi_SetSlaveSelect(&Spi, SPI_DESELECT_ALL);

    if (status != XST_SUCCESS) {
        xil_printf("# SPI transfer failed (%d)\r\n", status);
        return 0;
    }

#if TMAG_DEBUG_FRAMES
    xil_printf("# tx %02X %02X %02X %02X -> rx %02X %02X %02X %02X\r\n",
               tx[0], tx[1], tx[2], tx[3], rx[0], rx[1], rx[2], rx[3]);
#endif

    return ((u32)rx[0] << 24) | ((u32)rx[1] << 16) |
           ((u32)rx[2] <<  8) | ((u32)rx[3] <<  0);
}

/* Frame layout, datasheet Fig 7-10:
 * [31] R/W  [30:24] address  [23:8] data  [7:4] CMD  [3:0] CRC */
static u32 tmag_frame(u8 rw, u8 addr, u16 data, u8 cmd, u8 crc)
{
    return ((u32)(rw   & 0x01) << 31) |
           ((u32)(addr & 0x7F) << 24) |
           ((u32)(data)        <<  8) |
           ((u32)(cmd  & 0x0F) <<  4) |
           ((u32)(crc  & 0x0F) <<  0);
}

static void tmag_write(u8 addr, u16 data)
{
    tmag_xfer(tmag_frame(0, addr, data, 0, 0));
}

/* Reply is 8 status | 16 data | 4 status | 4 CRC */
static u16 tmag_read(u8 addr)
{
    u32 resp = tmag_xfer(tmag_frame(1, addr, 0x0000, 0, 0));
    return (u16)((resp >> 8) & 0xFFFF);
}

/* Datasheet Eq 1, scaled by 100: B = raw * RANGE / 32768 */
static s32 tmag_to_mT_x100(u16 raw)
{
    return ((s32)(s16)raw * RANGE_MT_X100) / 32768;
}

/* Datasheet Eq 3, scaled by 100. TEMP_RESULT is a plain unsigned binary
 * count (not 2's complement like the magnetic axes) -- the sign comes from
 * the subtraction below, so diff must be signed. */
static s32 tmag_temp_to_C_x100(u16 raw)
{
    s32 diff = (s32)raw - T_ADC_T0;
    return T_SENS_T0_X100 + (diff * 100) / T_ADC_RES_LSB_PER_C;
}

/* Exact integer sqrt, no FPU or libm needed. */
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

/* Sign handled separately: -50/100 is 0 in C, which would drop the minus. */
static void print_x100(s32 v)
{
    s32 whole = v / 100;
    s32 frac  = v % 100;

    if (frac < 0) {
        frac = -frac;
    }
    if (v < 0 && whole == 0) {
        xil_printf("-0.%02d", (int)frac);
    } else {
        xil_printf("%d.%02d", (int)whole, (int)frac);
    }
}

static void print_sample(u16 rx, u16 ry, u16 rz, u16 rt,
                         s32 bx, s32 by, s32 bz, s32 mag, s32 tempC)
{
#if OUTPUT_CSV
    (void)rx; (void)ry; (void)rz; (void)rt;
    print_x100(bx);    xil_printf(", ");
    print_x100(by);    xil_printf(", ");
    print_x100(bz);    xil_printf(", ");
    print_x100(mag);   xil_printf(", ");
    print_x100(tempC); xil_printf("\r\n");
#else
    xil_printf("raw %04X %04X %04X T %04X | Bx ", rx, ry, rz, rt);
    print_x100(bx);
    xil_printf("  By ");
    print_x100(by);
    xil_printf("  Bz ");
    print_x100(bz);
    xil_printf(" mT | |B| ");
    print_x100(mag);
    xil_printf(" mT | T ");
    print_x100(tempC);
    xil_printf(" C\r\n");
#endif
}

/* SPI mode 0: set neither CLK_ACTIVE_LOW nor CLK_PHASE_1.
 * Manual slave select keeps CS low for the whole frame. */
static int spi_init(void)
{
    XSpi_Config *cfg;
    int status;

    cfg = XSpi_LookupConfig(SPI_LOOKUP_ARG);
    if (cfg == NULL) {
        return XST_FAILURE;
    }

    status = XSpi_CfgInitialize(&Spi, cfg, cfg->BaseAddress);
    if (status != XST_SUCCESS) {
        return status;
    }

    status = XSpi_SetOptions(&Spi, XSP_MASTER_OPTION |
                                   XSP_MANUAL_SSELECT_OPTION);
    if (status != XST_SUCCESS) {
        return status;
    }

    XSpi_Start(&Spi);
    XSpi_IntrGlobalDisable(&Spi);
    XSpi_SetSlaveSelect(&Spi, SPI_DESELECT_ALL);

    if (Spi.DataWidth != XSP_DATAWIDTH_BYTE) {
        xil_printf("# IP transaction width is %d bits, expected 8\r\n",
                   Spi.DataWidth);
    }

    return XST_SUCCESS;
}


int main(void)
{
    u16 rawX, rawY, rawZ, rawT, readback;
    s32 bx, by, bz, tempC;
    u32 mag;

    if (spi_init() != XST_SUCCESS) {
        xil_printf("# SPI init failed\r\n");
        return -1;
    }

    tmag_xfer(CMD_DISABLE_CRC);
    usleep(1000);

    tmag_write(REG_SENSOR_CONFIG, SENSOR_CONFIG_XYZ);

    readback = tmag_read(REG_SENSOR_CONFIG);
    if (readback != SENSOR_CONFIG_XYZ) {
        xil_printf("# SENSOR_CONFIG readback 0x%04X, expected 0x%04X\r\n",
                   readback, SENSOR_CONFIG_XYZ);
    }

    /* Single write: averaging, active mode and the temperature channel all
     * land together. Splitting this into two writes would clobber CONV_AVG. */
    tmag_write(REG_DEVICE_CONFIG, DEVICE_CONFIG_ACTIVE);
    usleep(1000);

    readback = tmag_read(REG_DEVICE_CONFIG);
    if (readback != DEVICE_CONFIG_ACTIVE) {
        xil_printf("# DEVICE_CONFIG readback 0x%04X, expected 0x%04X\r\n",
                   readback, DEVICE_CONFIG_ACTIVE);
    }

#if OUTPUT_CSV
    xil_printf("Bx,By,Bz,Bmag,TempC\r\n");
#else
    xil_printf("\r\nTMAG5170 on Cmod S7\r\n");
    xil_printf("DEVICE_CONFIG readback = 0x%04X\r\n", readback);
#endif

    while (1) {
        rawX = tmag_read(REG_X_CH_RESULT);
        rawY = tmag_read(REG_Y_CH_RESULT);
        rawZ = tmag_read(REG_Z_CH_RESULT);
        rawT = tmag_read(REG_TEMP_RESULT);

        bx = tmag_to_mT_x100(rawX);
        by = tmag_to_mT_x100(rawY);
        bz = tmag_to_mT_x100(rawZ);
        tempC = tmag_temp_to_C_x100(rawT);

        mag = isqrt_u32((u32)(bx * bx) + (u32)(by * by) + (u32)(bz * bz));

        print_sample(rawX, rawY, rawZ, rawT,
                     bx, by, bz, (s32)mag, tempC);

        usleep(SAMPLE_PERIOD_US);
    }

    return 0;
}