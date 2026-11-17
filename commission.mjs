import '@matter/nodejs';
import '@matter/nodejs-ble';
import { Environment, Logger, LogLevel, Seconds } from '@matter/general';
import { MatterNode } from '@matter/nodejs-shell/dist/esm/MatterNode.js';
import { ManualPairingCodeCodec, VendorId } from '@matter/types';
import { GeneralCommissioning } from '@matter/types/clusters/general-commissioning';

process.umask(0o077);
const output = process.stdout.write.bind(process.stdout);
const status = (value) => output(`${value}\n`);
if (process.argv[2] === 'check') {
    status('matter-ready');
    process.exit(0);
}

// The stock interactive shell writes command history. Use its controller API,
// not its shell, and never forward SDK diagnostics that may contain inputs.
process.stdout.write = () => true;
process.stderr.write = () => true;
Environment.default.vars.set('ble.enable', true);
Logger.level = LogLevel.FATAL;
const log = Logger.destinations.default;
log.facilityLevels = { ControllerCommissioner: LogLevel.INFO };
log.add = ({ values, facility }) => {
    if (facility !== 'ControllerCommissioner' || typeof values?.[0] !== 'string') return;
    const step = /^Executing commissioning step (\d{1,2}\.\d{1,2}): /.exec(values[0]);
    if (step) status(`matter-step-${step[1]}`);
};
const disallowed = new Set(['TrustedAsTestCertificate', 'CdSignerVerificationSkipped',
    'CertificationTypeTest', 'CertificationTypeProvisional']);
let stage = 'input';
let node;
let success = false;
try {
    let input = '';
    for await (const chunk of process.stdin) {
        input += chunk;
        if (input.length > 4096) throw new Error('Input too long');
    }
    const { code, ssid, password, iface } = JSON.parse(input);
    const { passcode, shortDiscriminator } = ManualPairingCodeCodec.decode(code);
    stage = 'start';
    node = new MatterNode(71, iface);
    await node.initialize(false);
    await node.start();
    status('controller-started');

    stage = 'discovery';
    const candidates = await node.node.peers.discover({
        vendorId: VendorId(4442), productId: 71, timeout: Seconds(38),
        discoveryCapabilities: { ble: true, onIpNetwork: false },
    });
    const matches = candidates.filter(({ state: { commissioning: c } }) =>
        c.vendorId === 4442 && c.productId === 71 &&
        Number.isInteger(c.discriminator) && (c.discriminator >> 8) === shortDiscriminator);
    if (matches.length !== 1) throw new Error('Expected one matching advertiser');
    status('nanoleaf-ble-match');

    stage = 'commission';
    await node.certificateService();
    await matches[0].commission({
        passcode, autoSubscribe: false,
        regulatoryLocation: GeneralCommissioning.RegulatoryLocationType.Outdoor,
        regulatoryCountryCode: 'XX',
        wifiNetwork: { wifiSsid: ssid, wifiCredentials: password },
        onAttestationFailure(findings) {
            if (findings.some(({ level, type }) => level === 'error' || disallowed.has(type))) {
                status('production-attestation-rejected');
                return false;
            }
            return true;
        },
    });
    if (!matches[0].peerAddress) throw new Error('Missing commissioned peer');
    status('matter-wifi-commissioned');
    success = true;
} catch {
    status(`failed-${stage}`);
} finally {
    await node?.close();
    process.exit(success ? 0 : 1);
}
