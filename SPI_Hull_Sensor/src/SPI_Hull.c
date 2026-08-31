/*
 * tmag5170_cmods7.c
 * ---------------------------------------------------------------
 * Minimal bare-metal example: talk to a TMAG5170 3D Hall sensor
 * from a MicroBlaze on a Digilent Cmod S7-25 (Spartan-7 XC7S25),
 * using an AXI Quad SPI IP in the fabric.
 *
 *   1. Disables the sensor's CRC  (so you don't need CRC math yet)
 *   2. Enables the X, Y, Z channels
 *   3. Puts the sensor in continuous measurement mode
 *   4. Loops: reads X, Y, Z and prints them in milliTesla
 *
 * NO FLOATING POINT ANYWHERE. MicroBlaze has only 64 KB of block RAM
 * and floating-point printf alone can blow past that, so every value
 * is a fixed-point integer in hundredths of a milliTesla, printed
 * with the lightweight xil_printf.
 *
 * ---------------------------------------------------------------
 * WIRING -- Pmod JA to the TMAG5170UEVM header.
 * SPI names are role-based, so MOSI goes to MOSI (NOT crossed like UART):
 *
 *   JA1 (J2)  spi_ss    -> EVM CS
 *   JA2 (H2)  spi_mosi  -> EVM MOSI   (sensor pin SDI)
 *   JA3 (H4)  spi_miso  <- EVM MISO   (sensor pin SDO)
 *   JA4 (F3)  spi_sclk  -> EVM SCLK
 *   JA5       GND       -> EVM GND
 *   JA6       3.3V      -> EVM VCC
 *
 * ---------------------------------------------------------------
 * REQUIRED AXI QUAD SPI SETTINGS  (Vivado)
 *   SPI Mode ............. Standard
 *   Enable Master Mode ... checked
 *   Transaction Width .... 8       <-- this file assumes 8
 *   No. of Slaves ........ 1
 *   STARTUP Primitive .... UNCHECKED (needs USE_BOARD_FLOW = false first)
 *   Frequency Ratio ...... so that ext_spi_clk / ratio lands near 670 kHz
 *
 * Transaction Width 8 means each FIFO entry shifts out 8 bits, so one
 * 32-bit sensor frame costs 4 FIFO entries. The sensor cannot tell the
 * difference -- it only counts 32 SCK edges while CS is low. That is
 * why manual slave select matters: CS must NOT pulse between bytes.
 *
 * If you ever switch the IP to Transaction Width 32, this file breaks:
 * the driver would read the u8[4] buffer as a u32 and MicroBlaze's
 * little-endianness would reverse the byte order. Match the two.
 *
 * ---------------------------------------------------------------
 * VITIS BSP
 *   Set STDOUT to axi_uartlite_0, or nothing prints.
 *   No -lm and no floating point printf needed.
 *
 * TERMINAL: this build's AXI Uartlite is 128000 baud, NOT 115200
 * (xparameters.h: XPAR_AXI_UARTLITE_0_BAUDRATE 0x1F400 = 128000).
 * Open the serial terminal at 128000 or you get garbage characters.
 *
 * NOTE ON SPEED: this MicroBlaze runs at 10 MHz with no hardware
 * multiplier, divider, barrel shifter or FPU (XPAR_MICROBLAZE_USE_HW_MUL
 * / USE_DIV / USE_BARREL / USE_FPU are all 0). Every multiply, divide
 * and multi-bit shift below becomes a libgcc software routine. It is
 * entirely correct, just slow -- irrelevant at 5 readings per second.
 * If you later want speed, enabling the barrel shifter and hardware
 * multiplier in the MicroBlaze config costs a few hundred LUTs and
 * speeds up isqrt_u32() and the fixed-point math by roughly 10x.
 * ---------------------------------------------------------------
 */

#include "xparameters.h"
#include "xspi.h"
#include "xil_printf.h"
#include "sleep.h"

/* Set to 1 to print the raw bytes of every transfer. Invaluable when
 * you have a scope or logic analyzer on the Pmod: what prints here is
 * exactly what should appear on the wire, in this order. */
#define TMAG_DEBUG_FRAMES   0

/* ---------------- SPI setup ----------------
 * Vitis Unified (2023.2+): XSpi_LookupConfig() takes a BASE ADDRESS.
 * Classic Vitis / older SDK: it takes a DEVICE ID.
 * Pick whichever your xparameters.h actually defines.
 */
#define SPI_LOOKUP_ARG      XPAR_AXI_QUAD_SPI_0_BASEADDR
/* #define SPI_LOOKUP_ARG   XPAR_AXI_QUAD_SPI_0_DEVICE_ID */

/* The AXI Quad SPI driver uses a BITMASK for slave select:
 *     bit N set = slave N selected
 *     0x00      = all deselected
 * (The PS SPI driver XSpiPs used an index, where 0x00 meant "slave 0".
 *  Copying those values across is a classic silent failure.)
 */
#define SPI_SELECT_CS0      0x01
#define SPI_DESELECT_ALL    0x00

/* ---------------- TMAG5170 register offsets ----------------
 * These are ADDRESSES (which register), not bit positions.
 * Datasheet sbasaf4.pdf, Table 7-4.
 */
#define REG_DEVICE_CONFIG   0x00
#define REG_SENSOR_CONFIG   0x01
#define REG_SYSTEM_CONFIG   0x02
#define REG_CONV_STATUS     0x08
#define REG_X_CH_RESULT     0x09
#define REG_Y_CH_RESULT     0x0A
#define REG_Z_CH_RESULT     0x0B
#define REG_AFE_STATUS      0x0D
#define REG_TEST_CONFIG     0x0F

/* Full-scale magnetic range, in HUNDREDTHS of a milliTesla.
 * X/Y/Z_RANGE = 00b is +/-50 mT on a TMAG5170A1, so 50.00 mT -> 5000.
 * (It would be 15000 for the A2 half of the EVM.)
 */
#define RANGE_MT_X100       5000

static XSpi Spi;


/* =====================================================================
 * Layer 1 -- push 32 bits out, catch 32 bits coming back
 * =====================================================================
 * SPI is full duplex: while our 32 bits go out on MOSI, the sensor's
 * 32-bit answer arrives on MISO at the same time. The TMAG5170 uses
 * "in-frame communication", so a register we ask for comes back inside
 * THIS same frame -- no second dummy frame needed.
 *
 * With Transaction Width 8, XSpi_Transfer walks the u8 buffer one byte
 * per FIFO entry, MSB of each byte first. tx[0] therefore goes out on
 * the very first clock edge, which is why we pack bit 31 into it.
 *
 * CS must stay low for all 32 clocks. Manual slave select is what makes
 * that true: we assert CS once, run all four bytes, release once. With
 * automatic slave select the core pulses CS between FIFO entries and
 * the sensor would see four 8-clock frames instead of one 32-clock
 * frame -- every one the wrong length, all rejected, FRAME_STAT set.
 */
static u32 tmag_xfer(u32 frame)
{
    u8 tx[4], rx[4];
    int status;

    /* MSB first: bit 31 leaves on the very first clock edge.
     * These explicit shifts are endian-independent -- we are choosing
     * the byte order ourselves rather than inheriting the CPU's. */
    tx[0] = (u8)(frame >> 24);
    tx[1] = (u8)(frame >> 16);
    tx[2] = (u8)(frame >>  8);
    tx[3] = (u8)(frame >>  0);

    rx[0] = rx[1] = rx[2] = rx[3] = 0;

    XSpi_SetSlaveSelect(&Spi, SPI_SELECT_CS0);      /* CS low     */
    status = XSpi_Transfer(&Spi, tx, rx, 4);        /* 32 clocks  */
    XSpi_SetSlaveSelect(&Spi, SPI_DESELECT_ALL);    /* CS high    */

    if (status != XST_SUCCESS) {
        xil_printf("SPI transfer failed (%d)\r\n", status);
        return 0;
    }

#if TMAG_DEBUG_FRAMES
    xil_printf("  tx %02X %02X %02X %02X  ->  rx %02X %02X %02X %02X\r\n",
               tx[0], tx[1], tx[2], tx[3],
               rx[0], rx[1], rx[2], rx[3]);
#endif

    return ((u32)rx[0] << 24) | ((u32)rx[1] << 16) |
           ((u32)rx[2] <<  8) | ((u32)rx[3] <<  0);
}


/* =====================================================================
 * Layer 2 -- build a legal 32-bit frame
 * =====================================================================
 * Datasheet Figure 7-10, laid out MSB first:
 *
 *   bit 31      R/W       1 = read, 0 = write
 *   bits 30..24 address   7 bits -- which register
 *   bits 23..8  data      16 bits -- ignored by the chip on a read
 *   bits 7..4   CMD       read type / trigger a conversion
 *   bits 3..0   CRC       4-bit check, ignored once CRC is disabled
 *
 * Each field is shifted to its position and OR-ed in, so they merge
 * without disturbing each other. Sanity check: tmag_frame(0,0x0F,
 * 0x0004,0,7) reproduces TI's published 0x0F000407 exactly.
 */
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

static u16 tmag_read(u8 addr)
{
    /* Data bits are zero so the frame is deterministic -- which also
     * means the CRC for a given read address is a constant you could
     * precompute later instead of running a CRC engine every time.
     *
     * Reply frame: 8 status bits | 16 data bits | 4 status | 4 CRC
     * so the register contents live in bits 23..8 of what came back. */
    u32 resp = tmag_xfer(tmag_frame(1, addr, 0x0000, 0, 0));
    return (u16)((resp >> 8) & 0xFFFF);
}


/* =====================================================================
 * Layer 3 -- raw counts to fixed-point milliTesla
 * =====================================================================
 * Datasheet Equation 1. The register holds a signed (2's complement)
 * 16-bit number spanning the full +/- range:
 *
 *     B = (raw / 2^16) * 2 * RANGE   ==   raw * RANGE / 32768
 *
 * Scaled by 100 so the result is hundredths of a milliTesla and stays
 * an integer. Worst case 32767 * 5000 = 163,835,000 -- comfortably
 * inside a signed 32-bit int.
 */
static s32 tmag_to_mT_x100(u16 raw)
{
    return ((s32)(s16)raw * RANGE_MT_X100) / 32768;
}

/* Integer square root, bit-by-bit. Exact for all u32 inputs. No FPU,
 * no math library, no -lm. */
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

/* Print a fixed-point hundredths value as signed decimal. The sign is
 * handled separately because -50/100 is 0 in C, which would otherwise
 * turn -0.50 mT into "0.50". */
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


/* =====================================================================
 * SPI controller init
 * =====================================================================
 * SPI Mode 0 (CPOL = 0, CPHA = 0): the sensor changes SDO on the
 * falling edge of SCK and latches SDI on the rising edge (datasheet
 * Sec 7.5.2.1). Mode 0 is what you get by setting NEITHER
 * XSP_CLK_ACTIVE_LOW_OPTION (CPOL=1) nor XSP_CLK_PHASE_1_OPTION (CPHA=1).
 *
 * No prescaler call: the AXI Quad SPI has no runtime clock divider.
 * SCK = ext_spi_clk / Frequency Ratio, both fixed in Vivado.
 */
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

    /* Master, and we drive CS ourselves so it is guaranteed to stay low
     * across the whole 32-bit frame. Mode 0 = no clock options set. */
    status = XSpi_SetOptions(&Spi, XSP_MASTER_OPTION |
                                   XSP_MANUAL_SSELECT_OPTION);
    if (status != XST_SUCCESS) {
        return status;
    }

    XSpi_Start(&Spi);             /* enables the core                  */
    XSpi_IntrGlobalDisable(&Spi); /* required for polled XSpi_Transfer */

    XSpi_SetSlaveSelect(&Spi, SPI_DESELECT_ALL);

    /* If this reports something other than 8, the IP and this file
     * disagree about buffer width and every frame will be garbled. */
    if (Spi.DataWidth != XSP_DATAWIDTH_BYTE) {
        xil_printf("WARNING: IP transaction width is %d bits, "
                   "this code expects 8\r\n", Spi.DataWidth);
    }

    return XST_SUCCESS;
}


/* ===================================================================== */
int main(void)
{
    u16 rawX, rawY, rawZ, readback;
    s32 bx, by, bz;
    u32 mag;

    xil_printf("\r\nTMAG5170 on Cmod S7 / MicroBlaze\r\n");

    if (spi_init() != XST_SUCCESS) {
        xil_printf("SPI init failed\r\n");
        return -1;
    }

    /* --- Step 1: turn the CRC off -------------------------------------
     * CRC is ON at power-up and the chip silently ignores every command
     * whose CRC is wrong, so nothing works until you either compute
     * CRCs or disable them. This word is printed in the datasheet
     * (Sec 7.5.2.5) and already carries its own correct CRC of 0x7:
     *
     *   0x0F000407
     *    ^ ^^  ^^^^ ^
     *    | |   |    +-- CRC   = 0x7
     *    | |   +------- data  = 0x0004  (bit 2 = CRC_DIS = 1)
     *    | +----------- addr  = 0x0F    (TEST_CONFIG)
     *    +------------- R/W   = 0       (write)
     *
     * On the wire that is bytes 0F 00 04 07, in that order.
     */
    tmag_xfer(0x0F000407);
    usleep(1000);

    /* --- Step 2: enable the X, Y and Z channels ------------------------
     * SENSOR_CONFIG (0x01): MAG_CH_EN = 0x7 ("XYZ") sits in bits 9..6,
     * so 0x7 << 6 = 0x01C0. X/Y/Z_RANGE stay 00b = +/-50 mT.
     * Frame: 0x0101C000  ->  bytes 01 01 C0 00
     */
    tmag_write(REG_SENSOR_CONFIG, 0x01C0);

    /* Sanity check: read it straight back. Correct wiring gives 0x01C0.
     *   0x0000 -> MISO not connected, or CS never asserting
     *             (check SPI_SELECT_CS0 is 0x01, not 0x00)
     *   0xFFFF -> MISO floating
     * Fix the link before trusting any magnetic reading. */
    readback = tmag_read(REG_SENSOR_CONFIG);
    xil_printf("SENSOR_CONFIG readback = 0x%04X (expect 0x01C0)\r\n",
               readback);

    /* --- Step 3: start continuous conversions --------------------------
     * DEVICE_CONFIG (0x00): OPERATING_MODE = 0x2 ("Active measure mode")
     * sits in bits 6..4, so 0x2 << 4 = 0x0020.
     * Frame: 0x00002000  ->  bytes 00 00 20 00
     */
    tmag_write(REG_DEVICE_CONFIG, 0x0020);
    usleep(1000);

    /* --- Step 4: read forever ------------------------------------------
     * With DATA_TYPE = 000b each frame carries one register, so three
     * axes cost three frames = 12 bytes = 96 SCK cycles.
     */
    while (1) {
        rawX = tmag_read(REG_X_CH_RESULT);
        rawY = tmag_read(REG_Y_CH_RESULT);
        rawZ = tmag_read(REG_Z_CH_RESULT);

        bx = tmag_to_mT_x100(rawX);
        by = tmag_to_mT_x100(rawY);
        bz = tmag_to_mT_x100(rawZ);

        /* Each term is at most 5000^2 = 25e6, so the sum tops out at
         * 75e6 -- well inside u32. sqrt of a value scaled by 100^2
         * comes back scaled by 100, exactly what we want. */
        mag = isqrt_u32((u32)(bx * bx) + (u32)(by * by) + (u32)(bz * bz));

        xil_printf("raw %04X %04X %04X | Bx ", rawX, rawY, rawZ);
        print_x100(bx);
        xil_printf("  By ");
        print_x100(by);
        xil_printf("  Bz ");
        print_x100(bz);
        xil_printf(" mT | |B| ");
        print_x100((s32)mag);
        xil_printf(" mT\r\n");

        usleep(200000);   /* 5 readings per second */
    }

    return 0;
}