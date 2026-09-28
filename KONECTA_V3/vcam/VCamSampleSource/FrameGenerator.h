#pragma once

// Contrato com o KONECTA (app_central/capture/camera_virtual.py): um cabeçalho
// de 64 bytes seguido dos pixels BGRA, numa memória compartilhada em Global\.
// O KONECTA escreve; esta DLL lê. Mudou aqui, muda lá.
#define KONECTA_FRAME_NAME L"Global\\KonectaVCamFrame"
#define KONECTA_FRAME_MAGIC 0x3143564B // "KVC1"
#define KONECTA_FRAME_HEADER 64
#define KONECTA_FRAME_MAX_W 1920
#define KONECTA_FRAME_MAX_H 1080
#define KONECTA_FRAME_SIZE (KONECTA_FRAME_HEADER + KONECTA_FRAME_MAX_W * KONECTA_FRAME_MAX_H * 4)
#define KONECTA_FRAME_STALE_MS 2000

struct KonectaFrameHeader
{
	UINT32 magic;
	UINT32 width;
	UINT32 height;
	UINT32 stride;
	volatile LONG64 sequence;    // ímpar enquanto o KONECTA escreve
	volatile LONG64 timestampMs; // GetTickCount64 do último quadro completo
};

class FrameGenerator
{
	UINT _width;
	UINT _height;
	ULONGLONG _frame;
	MFTIME _prevTime;
	UINT _fps;
	HANDLE _deviceHandle;
	HANDLE _sharedMapping;
	const BYTE* _sharedView;
	ULONGLONG _sharedLastTry;
	wil::com_ptr_nothrow<ID3D11Texture2D> _texture;
	wil::com_ptr_nothrow<ID2D1RenderTarget> _renderTarget;
	wil::com_ptr_nothrow<ID2D1SolidColorBrush> _whiteBrush;
	wil::com_ptr_nothrow<IDWriteTextFormat> _textFormat;
	wil::com_ptr_nothrow<IDWriteFactory> _dwrite;
	wil::com_ptr_nothrow<IMFTransform> _converter;
	wil::com_ptr_nothrow<IWICBitmap> _bitmap;
	wil::com_ptr_nothrow<IMFDXGIDeviceManager> _dxgiManager;
	wil::com_ptr_nothrow<ID2D1Bitmap> _frameBitmap;

	HRESULT CreateRenderTargetResources(UINT width, UINT height);
	bool EnsureSharedFrame();
	bool DrawSharedFrame();

public:
	FrameGenerator() :
		_width(0),
		_height(0),
		_frame(0),
		_fps(0),
		_deviceHandle(nullptr),
		_sharedMapping(nullptr),
		_sharedView(nullptr),
		_sharedLastTry(0),
		_prevTime(MFGetSystemTime())
	{

	}

	~FrameGenerator()
	{
		if (_sharedView)
		{
			UnmapViewOfFile(_sharedView);
		}
		if (_sharedMapping)
		{
			CloseHandle(_sharedMapping);
		}
		if (_dxgiManager && _deviceHandle)
		{
			auto hr = _dxgiManager->CloseDeviceHandle(_deviceHandle); // don't report error at that point
			if (FAILED(hr))
			{
				WINTRACE(L"FrameGenerator CloseDeviceHandle: 0x%08X", hr);
			}
		}
	}

	HRESULT SetD3DManager(IUnknown* manager, UINT width, UINT height);
	const bool HasD3DManager() const;
	HRESULT EnsureRenderTarget(UINT width, UINT height);
	HRESULT Generate(IMFSample* sample, REFGUID format, IMFSample** outSample);
};
