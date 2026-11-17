import assert from 'node:assert/strict';
import test from 'node:test';
import path from 'node:path';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { ROOT } from '../demos/renovation-harness/adapter.mjs';
import {
  assertPrivateAndroidPaths, cpuEnvironment, fileSnapshot, prepareCpuRuntime
} from '../demos/renovation-harness/android-simulator-runtime.mjs';

const testRoot = path.join(ROOT, '.cache/renovation-agent/android-simulator-tests');
let fixture;

async function fixtureFile(source, relative, contents) {
  const location = path.join(source, relative);
  await mkdir(path.dirname(location), { recursive: true });
  await writeFile(location, contents);
}

async function makeFixture() {
  fixture = await mkdtemp(path.join(testRoot, 'case-'));
  const source = path.join(fixture, 'sdk/emulator');
  const runtime = path.join(fixture, 'runtime/37.2.12');
  await fixtureFile(source, 'source.properties', 'Pkg.Revision=37.2.12\nPkg.Path=emulator\n');
  await fixtureFile(source, 'emulator.exe', 'official emulator');
  await fixtureFile(source, 'qemu/windows-x86_64/qemu-system-x86_64.exe', 'official qemu');
  await fixtureFile(source, 'qemu/windows-x86_64/qemu-system-x86_64-headless.exe', 'official headless qemu');
  for (let index = 1; index <= 14; index += 1) await fixtureFile(source, `lib64/root-${index}.dll`, `root-${index}`);
  for (let index = 1; index <= 5; index += 1) await fixtureFile(source, `lib64/gles_angle/angle-${index}.dll`, `angle-${index}`);
  await fixtureFile(source, 'lib64/gles_angle/vk_swiftshader_icd.json', '{"library_path":"vk_swiftshader.dll"}\n');
  await fixtureFile(source, 'lib64/qt/lib/Qt6Core.dll', 'qt');
  return { source, runtime, sourceEmulator: source, runtimeRoot: runtime, sdkRoot: path.join(fixture, 'sdk') };
}

test.beforeEach(async () => { await mkdir(testRoot, { recursive: true }); });
test.afterEach(async () => {
  const relative = path.relative(testRoot, fixture);
  assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative));
  await rm(fixture, { recursive: true, force: true });
  fixture = undefined;
});

test('CPU environment is pure and normalizes PATH case while selecting SwiftShader', async () => {
  const paths = await makeFixture();
  const original = { PATH: 'D:\\existing;d:\\existing', Path: 'D:\\shadow', HOME: 'unchanged' };
  const android = { ...original, ANDROID_HOME: paths.sdkRoot, ANDROID_SDK_ROOT: paths.sdkRoot,
    ANDROID_USER_HOME: path.join(fixture, 'user'), ANDROID_EMULATOR_HOME: path.join(fixture, 'user'),
    ANDROID_AVD_HOME: path.join(fixture, 'avd'), GRADLE_USER_HOME: path.join(fixture, 'gradle'),
    TEMP: path.join(fixture, 'tmp'), TMP: path.join(fixture, 'tmp'), TMPDIR: path.join(fixture, 'tmp') };
  const before = JSON.stringify(android);
  const env = cpuEnvironment(android, { runtimeRoot: paths.runtime, androidEnvironment: android });
  assert.equal(env.ANGLE_DEFAULT_PLATFORM, 'swiftshader');
  assert.equal(Object.keys(env).filter(key => key.toLowerCase() === 'path').length, 1);
  const entries = env.PATH.split(';').map(value => value.toLowerCase());
  assert.equal(entries.filter(value => value === path.join(paths.runtime, 'lib64').toLowerCase()).length, 1);
  assert.equal(entries.filter(value => value === 'd:\\existing').length, 1);
  assert.equal(JSON.stringify(android), before);
  assert.equal(process.env.ANGLE_DEFAULT_PLATFORM, undefined);
});

test('preparation copies the pristine SDK and records repair identities', async () => {
  const paths = await makeFixture();
  const sourceBefore = await fileSnapshot(path.join(paths.source, 'emulator.exe'));
  const prepared = await prepareCpuRuntime(paths);
  assert.equal(prepared.reused, false);
  assert.equal((await stat(prepared.emulator)).size, sourceBefore.bytes);
  assert.equal((await fileSnapshot(path.join(paths.source, 'emulator.exe'))).sha256, sourceBefore.sha256);
  const manifest = JSON.parse(await readFile(prepared.manifestPath, 'utf8'));
  assert.equal(manifest.sdk_revision, '37.2.12');
  assert.equal(Object.keys(manifest.critical).length, 4);
  assert.equal(Object.keys(manifest.sdk_dlls).length, 20);
  assert.equal((await stat(path.join(paths.runtime, 'qemu/windows-x86_64/root-1.dll'))).size, 6);
});

test('valid runtime is reused and tampering is rejected without overwrite', async () => {
  const paths = await makeFixture();
  const first = await prepareCpuRuntime(paths);
  const target = path.join(paths.runtime, 'emulator.exe');
  const before = await readFile(target);
  const second = await prepareCpuRuntime(paths);
  assert.equal(second.reused, true);
  assert.deepEqual(await readFile(target), before);
  await writeFile(target, 'tampered runtime');
  await assert.rejects(() => prepareCpuRuntime(paths), /critical identity mismatch/u);
  assert.equal(await readFile(target, 'utf8'), 'tampered runtime');
  assert.equal(first.manifestPath, second.manifestPath);
});

test('source DLL tampering fails closed instead of refreshing the runtime', async () => {
  const paths = await makeFixture();
  await prepareCpuRuntime(paths);
  await writeFile(path.join(paths.source, 'lib64/root-1.dll'), 'changed source');
  await assert.rejects(() => prepareCpuRuntime(paths), /SDK DLL identity mismatch/u);
  assert.equal(await readFile(path.join(paths.runtime, 'qemu/windows-x86_64/root-1.dll'), 'utf8'), 'root-1');
});

test('D-drive physical path guard rejects a profile that resolves outside D', async () => {
  const paths = await makeFixture();
  const user = path.join(fixture, 'user');
  const env = { ANDROID_HOME: paths.sdkRoot, ANDROID_SDK_ROOT: paths.sdkRoot,
    ANDROID_USER_HOME: user, ANDROID_EMULATOR_HOME: user, ANDROID_AVD_HOME: path.join(fixture, 'avd'),
    GRADLE_USER_HOME: path.join(fixture, 'gradle'), TEMP: path.join(fixture, 'tmp'),
    TMP: path.join(fixture, 'tmp'), TMPDIR: path.join(fixture, 'tmp') };
  for (const location of Object.values(env)) await mkdir(location, { recursive: true });
  const profile = path.join(user, 'adb-default-profile');
  await mkdir(profile, { recursive: true });
  const resolve = async location => location === 'C:/Users/11239/.android' ? profile
    : location === profile ? 'C:/outside-profile' : location;
  await assert.rejects(() => assertPrivateAndroidPaths(env, { realpathImpl: resolve,
    defaultProfile: profile, extraPaths: [paths.runtime] }), /physical D-drive/u);
  const validResolve = async location => location === 'C:/Users/11239/.android' ? profile : location;
  const result = await assertPrivateAndroidPaths(env, { realpathImpl: validResolve,
    defaultProfile: profile, extraPaths: [paths.runtime] });
  assert.equal(result.defaultPhysical, profile);
});
