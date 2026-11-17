"""ngspice simulations of the op-amp/MOSFET linear current-sink laser driver.

Run from anywhere:  python3 laser_sink.py [analysis ...]   (no arguments runs everything)
Netlists are written to netlists/, raw data to data/, figures to plots/.

Circuit (per laser, on the driver board):
  laser rail -> laser anode; laser cathode -> AO3400A drain; source -> Rsense -> local ground.
  LM358: +IN = set point, -IN = sense node through Rf, Cf from output to -IN, Rg in series with the gate.
  Set point: Pico PWM (3.3 V, controller ground) -> R1 -> vset; R2 and CSET from vset to local ground.
Controller side: laser-rail buck (ideal source + ripple), optional AO3401 high-side switch, 0.5 m cable.
The "atomizer" analysis covers the driver board's atomizer switch (Q2), which is driven from the same
PWM line through a diode/RC activity detector (R7, D1, C7, R8, R9).
"""

import itertools
import subprocess
import sys
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
NETDIR, DATADIR, PLOTDIR = HERE / "netlists", HERE / "data", HERE / "plots"
for d in (NETDIR, DATADIR, PLOTDIR):
    d.mkdir(exist_ok=True)

# rs: sense resistance; rail: laser-rail setting from the controller's buck.
# Config B bridges solder jumper JP1 (~1 mohm), putting R6 = 0.75 ohm in parallel with R1 = 7.5 ohm.
RS_A, RS_B_EXTRA, R_JUMPER = 7.5, 0.75, 1e-3
CONFIGS = {
    "A": dict(label="A: Sony SLD3134VL", rs=RS_A, rail=6.0, laser="LD_SONY"),
    "B": dict(label="B: Osram PLPT5 447KA", rs=RS_A * (RS_B_EXTRA + R_JUMPER) / (RS_A + RS_B_EXTRA + R_JUMPER),
              rail=5.0, laser="LD_OSRAM"),
}

R1, R2, CSET, VPWM = 15e3, 1e3, 10e-6, 3.3
VSET_FS = VPWM * R2 / (R1 + R2)

RF, CF, RG = 1e3, 10e-9, 470

# Gate capacitance added to the AO3400 model, which has only 225 pF Cgs and no Cgd.
# Datasheet: Ciss 630 pF, Crss 50 pF at Vds = 15 V (larger at low Vds).
CGS_EXTRA, CGD_EXTRA = 300e-12, 100e-12

# Looser solver settings for cases that drive the LM358 model hard (supply collapse, saturation).
ROBUST_OPTS = "reltol=1e-3 abstol=1e-8 vntol=1e-5 method=gear itl4=200 rshunt=1e9"

# LM358 worst-case input offset at 25 C (datasheet max).
VOS_MAX = 7e-3

MODELS = f"""
.include {HERE / 'models' / 'LM358.mod'}
.include {HERE / 'models' / 'AO340x.lib'}

* Laser diode electrical models (anode, cathode), fitted to datasheet forward-voltage points.
* Osram PLPT5 447KA: 3.7 V at 0.3 A, 4.6 V at 1.2 A (typ).
.subckt LD_OSRAM a k
D1 a k DOSR
.model DOSR D(IS=1.5e-10 N=6.28 RS=0.75 CJO=200p)
.ends
* High-Vf corner: about +0.6 V at 0.3 A.
.subckt LD_OSRAM_HI a k
V1 a a1 0.3
D1 a1 k DOSRH
.model DOSRH D(IS=1.5e-10 N=6.28 RS=1.83 CJO=200p)
.ends
* Sony SLD3134VL: 4.8 V at 30 mA (typ), 5.2 V max.
.subckt LD_SONY a k
D1 a k DSONY
.model DSONY D(IS=2.9e-12 N=7.5 RS=10 CJO=20p)
.ends
.subckt LD_SONY_HI a k
V1 a a1 0.4
D1 a1 k DSONYH
.model DSONYH D(IS=2.9e-12 N=7.5 RS=10 CJO=20p)
.ends

.model BAT54 D(IS=2e-7 RS=1.5 N=1.03 CJO=10p BV=30 IBV=2u TT=5n)
.model SWI SW(VT=1.5 VH=0.1 RON=2 ROFF=1e9)
"""


def deck(cfg, analysis, *, vrail=None, laser_variant="", vset_src=None, pwm_src=None,
         ripple=0.0, hot_switch=None, laser_connect=None, cf=CF, rf=RF, rg=RG, cgd=CGD_EXTRA,
         cgs=CGS_EXTRA, vos=0.0, rs=None, r1=R1, r2=R2, ic="", title="laser sink",
         options="reltol=1e-4 abstol=1e-10 vntol=1e-7 method=gear"):
    """Build an ngspice deck.

    Set point: either vset_src (a source spec forced directly on the vset node, bypassing the PWM
    filter) or pwm_src (a source spec for the Pico PWM pin, which drives the R1/R2/CSET network).
    hot_switch: (t_on, t_off) for the controller's AO3401 high-side switch; None = rail always on.
    laser_connect: time at which the laser is connected to the driver; None = always connected.
    vos: extra op-amp input offset, in series with +IN.
    """
    c = CONFIGS[cfg]
    vrail = c["rail"] if vrail is None else vrail
    rs = c["rs"] if rs is None else rs
    laser = c["laser"] + laser_variant
    L = [f"* {title}", MODELS]

    L.append(f"Vsup sup0 0 DC {vrail}")
    L.append(f"Vrip sup sup0 SIN(0 {ripple / 2} 500k)")
    if hot_switch is None:
        L.append("Rsw sup swout 1m")
    else:
        t_on, t_off = hot_switch
        L.append("XQsw swout gsw sup AO3401")
        L.append("Rpull sup gsw 100k")
        L.append("Rgsw gsw nd 100k")
        L.append("Ssw nd 0 en 0 SWI")
        L.append(f"Ven en 0 PULSE(0 3.3 {t_on} 1u 1u {t_off - t_on} 1)")
        L.append("Cbulk sup 0 220u")

    L += ["Rc1 swout c1 50m", "Lc1 c1 rail 0.5u", "Rc2 gl c2 50m", "Lc2 c2 0 0.5u"]

    L += ["Cin rail cin 10u", "Rcin cin gl 10m", "Cdec rail gl 100n"]
    if laser_connect is None:
        L.append("Vil rail la 0")
    else:
        # Contact closes as a conductance ramp from 0 to 1000 S over 100 ns.
        L.append(f"Bld rail ra I = V(rail,ra) * 1e3 * min(max((time - {laser_connect}) / 100n, 0), 1)")
        L.append("Rldleak rail ra 1e9")
        L.append("Lld ra rb 50n")
        L.append("Vil rb la 0")
    L.append(f"XLD la d {laser}")
    L += ["Dprot d rail BAT54", "Cld rail d 10n"]
    L += ["XM d g s AO3400", f"Cgsx g s {cgs}", f"Cgdx g d {cgd}", f"Rs s gl {rs}"]
    L += ["XU1 inp inn rail gl oa LM358", f"Rg oa g {rg}", f"Cf oa inn {cf}", f"Rf s inn {rf}"]
    L.append(f"Vos inp vset DC {vos}")

    if vset_src is not None:
        L.append(f"Vset vset gl {vset_src}")
    else:
        L.append(f"Vpwm pwm 0 {pwm_src if pwm_src else f'DC {VPWM}'}")
        L += [f"Rp1 pwm vset {r1}", f"Rp2 vset gl {r2}", f"Cp vset gl {CSET}"]

    if ic:
        L.append(ic)
    L.append(f".options {options}")
    L.append(analysis)
    L.append(".end")
    return "\n".join(L) + "\n"


def run(name, netlist, control, allow_abort=False):
    """Run ngspice in batch mode; control lines write data/<name>.txt via $OUT.

    With allow_abort, an analysis started from the control block that stops early (e.g. timestep too
    small) still returns the data computed up to that point.
    """
    out = DATADIR / f"{name}.txt"
    out.unlink(missing_ok=True)
    body = netlist.replace("\n.end\n", "\n")
    body += ".control\nset wr_singlescale\nset wr_vecnames\noption numdgt=9\n"
    body += "\n".join(l.replace("$OUT", str(out)) for l in control) + "\n.endc\n.end\n"
    path = NETDIR / f"{name}.cir"
    path.write_text(body)
    res = subprocess.run(["ngspice", "-b", str(path)], capture_output=True, text=True)
    aborted = "Timestep too small" in res.stdout + res.stderr
    if not out.exists() or (not allow_abort and (res.returncode != 0 or aborted)):
        print(res.stdout[-3000:], res.stderr[-3000:])
        raise RuntimeError(f"ngspice failed for {name}")
    with open(out) as f:
        names = f.readline().lower().split()
    data = np.loadtxt(out, skiprows=1, ndmin=2)
    d = {n: data[:, i] for i, n in enumerate(names)}
    d["aborted"] = aborted
    return d


def save(fig, name):
    fig.tight_layout()
    fig.savefig(PLOTDIR / f"{name}.png", dpi=130)
    plt.close(fig)
    print(f"  wrote plots/{name}.png")


def a_models():
    """Laser diode I-V fits against datasheet points."""
    pts = {"LD_OSRAM": [(0.3, 3.7), (1.2, 4.6)], "LD_SONY": [(0.030, 4.8)]}
    hi = {"LD_OSRAM": [(1.2, 5.9)], "LD_SONY": [(0.030, 5.2)]}
    fig, axs = plt.subplots(1, 2, figsize=(10, 4))
    for ax, (cfg, imax) in zip(axs, [("A", 0.06), ("B", 1.4)]):
        name = CONFIGS[cfg]["laser"]
        for var, ls in [("", "-"), ("_HI", "--")]:
            nl = f"* iv\n{MODELS}\nIt 0 a DC 0\nXLD a 0 {name}{var}\n.dc It 0 {imax} {imax / 200}\n.end\n"
            d = run(f"iv_{name}{var}", nl, ["run", "wrdata $OUT v(a)"])
            ax.plot(d["v(a)"], d["i-sweep"] * 1e3, ls, label=f"model{' (high-Vf corner)' if var else ' (typical)'}")
        ax.plot([v for i, v in pts[name]], [i * 1e3 for i, v in pts[name]], "ko", label="datasheet / measured (typ)")
        ax.plot([v for i, v in hi[name]], [i * 1e3 for i, v in hi[name]], "rx", ms=9, label="datasheet max Vf")
        ax.set(xlabel="forward voltage (V)", ylabel="current (mA)", title=CONFIGS[cfg]["label"])
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    save(fig, "1_laser_models")


def a_transfer():
    """Laser current vs PWM duty (PWM represented by its average, through the real divider)."""
    fig, axs = plt.subplots(1, 2, figsize=(10, 4))
    for cfg in "AB":
        c = CONFIGS[cfg]
        nl = deck(cfg, f".dc Vpwm 0 {VPWM} {VPWM / 100}", pwm_src="DC 0")
        d = run(f"transfer_{cfg}", nl, ["run", "wrdata $OUT i(Vil)"])
        duty = d["v-sweep"] / VPWM * 100
        i = d["i(vil)"] * 1e3
        ideal = duty / 100 * VSET_FS / c["rs"] * 1e3
        axs[0].plot(duty, i, label=f"{c['label']} (Rs = {c['rs']} ohm)")
        axs[1].plot(duty[5:], (i[5:] - ideal[5:]) / ideal[-1] * 100, label=c["label"])
        print(f"  {cfg}: full scale {i[-1]:.2f} mA (ideal {ideal[-1]:.2f}); at 0% duty {i[0] * 1e3:.1f} uA")
    axs[0].set(xlabel="PWM duty (%)", ylabel="laser current (mA)", yscale="log", ylim=(0.1, 500),
               title="Laser current vs PWM duty (log scale)")
    axs[1].set(xlabel="PWM duty (%)", ylabel="error (% of full scale)", title="Deviation from ideal straight line")
    for ax in axs:
        ax.grid(alpha=0.3, which="both")
        ax.legend(fontsize=8)
    save(fig, "2_transfer")


def a_rail():
    """Laser current and MOSFET dissipation vs laser-rail voltage, at full-scale set point."""
    fig, axs = plt.subplots(2, 2, figsize=(10, 7))
    for col, cfg in enumerate("AB"):
        c = CONFIGS[cfg]
        for var, ls in [("", "-"), ("_HI", "--")]:
            nl = deck(cfg, ".dc Vsup 3 12 0.02", laser_variant=var)
            d = run(f"rail_{cfg}{var}", nl, ["run", "wrdata $OUT i(Vil) v(d) v(s) v(rail) v(gl)"])
            v, i = d["v-sweep"], d["i(vil)"]
            p = (d["v(d)"] - d["v(s)"]) * i
            final = i[-1]
            ok = v[np.argmax(i > 0.99 * final)]
            lab = "typical laser" if not var else "high-Vf laser"
            axs[0, col].plot(v, i * 1e3, ls, label=f"{lab}: in regulation above {ok:.2f} V")
            axs[1, col].plot(v, p * 1e3, ls, label=lab)
            print(f"  {cfg}{var}: within 1% above {ok:.2f} V; I(12 V)={final * 1e3:.2f} mA; "
                  f"line reg {(i[-1] - i[np.argmin(abs(v - (ok + 0.5)))]) / final * 100:.3f}% from {ok + 0.5:.1f}-12 V; "
                  f"P_mosfet at rail {c['rail']} V = {np.interp(c['rail'], v, p) * 1e3:.0f} mW")
        axs[0, col].axvline(c["rail"], color="gray", lw=0.8, ls=":")
        axs[1, col].axvline(c["rail"], color="gray", lw=0.8, ls=":")
        axs[0, col].set(title=c["label"], xlabel="laser rail (V)", ylabel="laser current (mA)")
        axs[1, col].set(xlabel="laser rail (V)", ylabel="MOSFET dissipation (mW)")
        for ax in axs[:, col]:
            ax.grid(alpha=0.3)
            ax.legend(fontsize=8)
    save(fig, "3_rail")


def a_softstart():
    """Turn-on and turn-off commanded by the PWM pin (0 -> 100% at 5 ms, back to 0% at 85 ms)."""
    fig, ax = plt.subplots(figsize=(8, 4))
    for cfg in "AB":
        c = CONFIGS[cfg]
        nl = deck(cfg, ".tran 20u 160m", pwm_src=f"PULSE(0 {VPWM} 5m 1u 1u 80m 1)")
        d = run(f"softstart_{cfg}", nl, ["run", "wrdata $OUT i(Vil)"])
        t, i = d["time"], d["i(vil)"]
        fs = i[np.argmin(abs(t - 84e-3))]
        ax.plot(t * 1e3, i / fs * 100, label=f"{c['label']} (100% = {fs * 1e3:.1f} mA)")
        t10, t90 = t[np.argmax(i > 0.1 * fs)], t[np.argmax(i > 0.9 * fs)]
        print(f"  {cfg}: 10-90% rise {1e3 * (t90 - t10):.1f} ms, overshoot {100 * (i.max() / fs - 1):.3f}%")
    ax.set(xlabel="time (ms)", ylabel="laser current (% of full scale)",
           title="Turn-on/off commanded by PWM (0 -> 100% duty at 5 ms, -> 0% at 85 ms)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    save(fig, "4_softstart")


def a_ripple():
    """Steady state at 50% PWM (100 kHz), without and with 50 mV p-p, 500 kHz ripple on the laser rail."""
    fig, axs = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    f = 100e3
    for ax, cfg in zip(axs, "AB"):
        c = CONFIGS[cfg]
        for rip in (0.0, 0.05):
            nl = deck(cfg, ".tran 20n 4m 3.9m 20n", pwm_src=f"PULSE(0 {VPWM} 0 5n 5n {0.5 / f} {1 / f})",
                      ripple=rip, ic=f".ic v(vset)={VSET_FS / 2}")
            d = run(f"ripple_{cfg}_{int(rip * 1e3)}mV", nl, ["run", "wrdata $OUT i(Vil) v(rail) v(gl)"])
            t, i = d["time"], d["i(vil)"]
            i_ac = i - np.polyval(np.polyfit(t, i, 1), t)  # remove the slow settling of the set-point filter
            mean, pp = i.mean(), np.ptp(i_ac)
            rail_pp = np.ptp(d["v(rail)"] - d["v(gl)"])
            lab = "PWM only" if rip == 0 else f"PWM + {rip * 1e3:.0f} mV p-p buck ripple"
            ax.plot((t - t[0]) * 1e6, i_ac / mean * 100, lw=0.8,
                    label=f"{lab}: {pp / mean * 100:.2f}% p-p")
            print(f"  {cfg} {lab}: mean {mean * 1e3:.2f} mA, ripple {pp * 1e6:.1f} uA p-p "
                  f"({pp / mean * 100:.3f}%), rail ripple at driver {rail_pp * 1e3:.2f} mV p-p")
        ax.set(xlim=(0, 30), xlabel="time (us)", ylabel="current ripple (% of mean)",
               title=f"{c['label']}: 50% PWM at 100 kHz")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    save(fig, "5_ripple")


def a_hotswitch():
    """Laser rail switched by the controller's AO3401: wrong order (set point already high) vs right order."""
    fig, axs = plt.subplots(2, 2, figsize=(11, 7))
    for col, cfg in enumerate("AB"):
        c = CONFIGS[cfg]
        for var, ls in [("", "-"), ("_HI", "--")]:
            lab = "typical laser" if not var else "high-Vf laser"
            # Wrong order: rail on at 1 ms while PWM is already at 100%.
            nl = deck(cfg, ".tran 50n 4m 0 50n", hot_switch=(1e-3, 1), laser_variant=var,
                      ic=f".ic v(vset)={VSET_FS}", options=ROBUST_OPTS)
            d = run(f"hot_wrong_{cfg}{var}", nl, ["run", "wrdata $OUT i(Vil)"])
            t, i = d["time"], d["i(vil)"]
            fs = i[-1]
            axs[0, col].plot((t - 1e-3) * 1e6, i * 1e3, ls, label=f"{lab}: peak {i.max() / fs * 100 - 100:.0f}% over")
            print(f"  {cfg}{var} wrong order: final {fs * 1e3:.2f} mA, peak {i.max() * 1e3:.2f} mA "
                  f"(overshoot {100 * (i.max() / fs - 1):.1f}%)")
            # Right order: rail on at 1 ms, PWM 0 -> 100% at 3 ms, PWM off at 45 ms, rail off at 75 ms.
            nl = deck(cfg, ".tran 1u 90m 0 1u", hot_switch=(1e-3, 75e-3), laser_variant=var,
                      pwm_src=f"PULSE(0 {VPWM} 3m 1u 1u 42m 1)", options=ROBUST_OPTS)
            d = run(f"hot_right_{cfg}{var}", nl, ["run", "wrdata $OUT i(Vil) v(rail) v(d)"])
            t, i = d["time"], d["i(vil)"]
            vrev = (d["v(d)"] - d["v(rail)"]).max()
            axs[1, col].plot(t * 1e3, i * 1e3, ls, label=f"{lab}: peak {i.max() / fs * 100:.1f}% of steady state")
            print(f"  {cfg}{var} right order: peak {i.max() * 1e3:.2f} mA = {100 * i.max() / fs:.2f}% of steady state "
                  f"{fs * 1e3:.2f} mA, max reverse voltage on laser {vrev:.3f} V")
        axs[0, col].set(xlim=(-20, 400), xlabel="time after rail switched on (us)", ylabel="laser current (mA)",
                        title=f"{c['label']}\nwrong order: rail on with set point already 100%")
        axs[1, col].set(xlabel="time (ms)", ylabel="laser current (mA)",
                        title="right order: rail on 1 ms, PWM on 3 ms,\nPWM off 45 ms, rail off 75 ms")
        for ax in axs[:, col]:
            ax.grid(alpha=0.3)
            ax.legend(fontsize=8)
    save(fig, "6_hotswitch")


def a_step():
    """Fast +/-10% set-point steps applied directly at +IN (bypassing the PWM filter): loop damping."""
    cases = [("nominal", {}), ("Cgd x3", dict(cgd=300e-12)), ("rail 12 V", dict(vrail=12.0)),
             ("Rg 100 ohm (for comparison)", dict(rg=100))]
    fig, axs = plt.subplots(1, 2, figsize=(11, 4))
    for ax, cfg in zip(axs, "AB"):
        c = CONFIGS[cfg]
        for name, kw in cases:
            v0, v1 = 0.9 * VSET_FS, VSET_FS
            nl = deck(cfg, ".tran 10n 300u 0 10n", vset_src=f"PULSE({v0} {v1} 50u 10n 10n 100u 1)", **kw)
            d = run(f"step_{cfg}_{name.split()[0]}{name.split()[1] if len(name.split()) > 1 else ''}", nl,
                    ["run", "wrdata $OUT i(Vil)"])
            t, i = d["time"], d["i(vil)"]
            i0, i1 = i[np.argmin(abs(t - 45e-6))], i[np.argmin(abs(t - 145e-6))]
            m = (t > 50e-6) & (t < 150e-6)
            os_ = (i[m].max() - i1) / (i1 - i0) * 100
            ax.plot((t - 50e-6) * 1e6, (i - i0) / (i1 - i0), label=f"{name}: overshoot {os_:.1f}%")
            print(f"  {cfg} {name}: overshoot {os_:.1f}%")
        ax.set(xlim=(-10, 150), xlabel="time after step (us)", ylabel="normalized current step",
               title=f"{c['label']}\n90% -> 100% -> 90% set-point step")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    save(fig, "7_step")


def a_plug():
    """Fault case: laser connected (at 1 ms) to a driver that is already powered with set point at 100%."""
    fig, axs = plt.subplots(1, 2, figsize=(11, 4))
    for ax, cfg in zip(axs, "AB"):
        c = CONFIGS[cfg]
        nl = deck(cfg, "", laser_connect=1e-3, ic=f".ic v(vset)={VSET_FS}",
                  options=ROBUST_OPTS)
        d = run(f"plug_{cfg}", nl, ["tran 5n 1.1m 0.99m 1n", "wrdata $OUT i(Vil) v(g)"], allow_abort=True)
        t, i = d["time"], d["i(vil)"]
        iset = VSET_FS / c["rs"]
        ax.plot((t - 1e-3) * 1e6, i * 1e3, label="laser current")
        ax.axhline(iset * 1e3, color="gray", ls=":", lw=0.8, label=f"set point {iset * 1e3:.1f} mA")
        end = (t[-1] - 1e-3) * 1e6
        if d["aborted"]:
            ax.axvline(end, color="r", ls="--", lw=0.8, label=f"LM358 model stops converging ({end:.2f} us)")
        print(f"  {cfg}: peak {i.max() * 1e3:.1f} mA vs set point {iset * 1e3:.2f} mA ({i.max() / iset:.1f}x); "
              f"{i[-1] * 1e3:.1f} mA at {end:.2f} us" + (" (simulation stopped: op-amp model convergence)"
                                                       if d["aborted"] else ""))
        ax.set(xlim=(-0.1, max(end * 1.2, 1)), xlabel="time after laser connects (us)", ylabel="laser current (mA)",
               title=f"{c['label']}: hot-plugging the laser (fault case)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    save(fig, "8_hotplug")


def a_tolerance():
    """Worst-case full-scale current: op-amp offset, 1% resistors, PWM high level 3.2-3.3 V."""
    for cfg in "AB":
        c = CONFIGS[cfg]
        res = []
        for vos, krs, kr1, kr2, vp in itertools.product([-VOS_MAX, VOS_MAX], [0.99, 1.01], [0.99, 1.01],
                                                        [0.99, 1.01], [3.2, 3.3]):
            nl = deck(cfg, ".op", vos=vos, rs=c["rs"] * krs, r1=R1 * kr1, r2=R2 * kr2, pwm_src=f"DC {vp}")
            d = run(f"tol_{cfg}", nl, ["op", "wrdata $OUT i(Vil)"])
            res.append(d["i(vil)"][0])
        nom = VSET_FS / c["rs"]
        print(f"  {cfg}: nominal {nom * 1e3:.2f} mA; worst case {min(res) * 1e3:.2f} to {max(res) * 1e3:.2f} mA "
              f"({(min(res) / nom - 1) * 100:+.1f}% / {(max(res) / nom - 1) * 100:+.1f}%)")


# Driver-board atomizer switch: PWM -> R7 (RDET) -> D1 -> (C7 || R8) -> R9 (RGA_ATOM) -> Q2 gate.
RDET, CDET, RAD, RGA_ATOM = 330, 47e-9, 220e3, 100
R_PIN = 40          # RP2040 GPIO output resistance at the 12 mA drive setting (approx.)
R_ATOM = 5.0 / 0.060  # atomizer modelled as a 60 mA resistive load on +5V


def a_atomizer():
    """Atomizer switch driven by the PWM activity detector: gate voltage vs duty, delays, pin droop."""
    f = 100e3
    t_on, t_off = 1e-3, 31e-3
    fig, axs = plt.subplots(1, 2, figsize=(11, 4))
    for duty in (0.01, 0.05, 0.5, 1.0):
        raw = f"DC {VPWM}" if duty == 1 else f"PULSE(0 {VPWM} 0 5n 5n {duty / f} {1 / f})"
        nl = f"""* atomizer activity detector, duty {duty}
{MODELS}
Vraw raw 0 {raw}
Bpin pin0 0 V = (time > {t_on}) * (time < {t_off}) * V(raw)
Rpin pin0 pin {R_PIN}
Rdrv pin 0 {R1 + R2}
Rdet pin a {RDET}
Ddet a k BAT54
Cdet k 0 {CDET}
Rad k 0 {RAD}
Rga k g {RGA_ATOM}
XQ d g 0 AO3400
Cgsx g 0 {CGS_EXTRA}
Cgdx g d {CGD_EXTRA}
Ratom v5 d {R_ATOM}
V5 v5 0 5
.options reltol=1e-4 method=gear
.tran 50n 80m 0 0.25u
.end
"""
        d = run(f"atomizer_{int(duty * 100)}pct", nl, ["run", "wrdata $OUT v(g) v(d) v(pin) v(raw)"])
        t, vg = d["time"], d["v(g)"]
        ia = (5 - d["v(d)"]) / R_ATOM
        m = (t > 25e-3) & (t < 30e-3)
        # time-weighted (the solver's timesteps cluster at the PWM edges)
        tw = np.linspace(25e-3, 30e-3, 2_000_001)
        hi = np.interp(tw, t, d["v(raw)"]) > VPWM / 2
        droop = VPWM - np.interp(tw, t, d["v(pin)"])[hi].mean()
        vg_mean = np.interp(tw, t, vg).mean()
        t90 = t[np.argmax((t > t_on) & (ia > 0.9 * 0.060))] - t_on
        after = t > t_off
        t10 = t[after][np.argmax(ia[after] < 0.1 * 0.060)] - t_off
        lab = f"{duty * 100:.0f}% duty"
        axs[0].plot(t * 1e3, vg, label=f"{lab}: gate {vg_mean:.2f} V")
        axs[1].plot(t * 1e3, ia * 1e3, label=f"{lab}: on {t90 * 1e3:.2f} ms, off {t10 * 1e3:.0f} ms")
        print(f"  {lab}: gate {vg_mean:.2f} V, atomizer {ia[m].mean() * 1e3:.1f} mA "
              f"(Vds {d['v(d)'][m].mean() * 1e3:.0f} mV), on in {t90 * 1e3:.2f} ms, off {t10 * 1e3:.0f} ms "
              f"after PWM stops, PWM-high droop {droop * 1e3:.1f} mV ({droop / VPWM * 100:.2f}% laser-current error)")
    axs[0].set(xlabel="time (ms)", ylabel="AO3400A gate (V)", title="Atomizer switch gate (PWM on 1-31 ms)")
    axs[1].set(xlabel="time (ms)", ylabel="atomizer current (mA)", title="Atomizer current (60 mA load)")
    for ax in axs:
        ax.axvspan(t_on * 1e3, t_off * 1e3, color="gray", alpha=0.08)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    save(fig, "9_atomizer")


ANALYSES = dict(models=a_models, transfer=a_transfer, rail=a_rail, softstart=a_softstart, ripple=a_ripple,
                hotswitch=a_hotswitch, step=a_step, plug=a_plug, tolerance=a_tolerance, atomizer=a_atomizer)

if __name__ == "__main__":
    for name in sys.argv[1:] or ANALYSES:
        print(name)
        ANALYSES[name]()
